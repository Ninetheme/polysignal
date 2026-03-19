"""PolySignal — %3 Garantili Kar Stratejisi.

Matematik:
  UP_bid + DN_bid < $1.00 → spread var
  Kar% = spread / pair_cost × 100
  Kar% >= 3 ise → esit cift al → GARANTI %3+ kar

Ornek:
  UP bid ¢48 + DN bid ¢49 = ¢97 | spread ¢3
  $100 / $0.97 = 103 cift → payout $103 → kar $3 (%3.09)

Kural:
  1. spread/pair_cost >= %3 olana kadar BEKLE
  2. Kosul saglaninca → esit cift al (shares %5 dengede)
  3. Her saniye kontrol, butce bitene kadar devam
  4. Her 5dk otomatik tekrarla
"""

import asyncio
import json
import os
import time
from typing import Optional

from config.settings import BotConfig
from core.btc_price_feed import BtcPriceFeed
from core.market_data import MarketDataFeed, OrderBook, LastTrade
from core.market_scanner import MarketScanner
from core.order_manager import OrderManager, OrderSide
from core.paper_engine import PaperPortfolio
from core.limit_engine import LimitEngine
from core.signal_tracker import SignalTracker, SignalState, HedgeRequest
from utils.logger import setup_logger

def _create_clob_client(config):
    """ClobClient oluştur. Credentials yoksa None döner (test mode)."""
    if not config.polymarket.private_key:
        return None
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(
            host=config.polymarket.clob_url,
            key=config.polymarket.api_key,
            chain_id=config.polymarket.chain_id,
            funder=config.polymarket.wallet_address,
            private_key=config.polymarket.private_key,
            creds={
                "apiKey": config.polymarket.api_key,
                "secret": config.polymarket.api_secret,
                "passphrase": config.polymarket.api_passphrase,
            },
        )
        return client
    except Exception as e:
        log = setup_logger("strategy")
        log.warning("ClobClient olusturulamadi (test mode): %s", e)
        return None

log = setup_logger("strategy")

import pathlib as _pathlib
_BASE = _pathlib.Path(__file__).resolve().parent.parent
ARCHIVE_DIR = str(_BASE / "data")
ARCHIVE_FILE = str(_BASE / "data" / "order_archive.json")
HISTORY_FILE = str(_BASE / "data" / "market_history.json")

# Strateji
MIN_PROFIT_PCT = 3.0    # minimum %3 kar olacak spread
MAX_PAIR_COST = 1.00    # bid pair cost >= ¢100 ise emir gonderme
MAX_LOSS_PCT = 10.0     # maksimum %10 zarar → tum emirler durdurulur
MAX_SH_GAP_PCT = 3.0    # shares max %3 fark
HEDGE_MIN_RATIO = 0.20  # zayif tarafa minimum %20

# Faz bazli butce (10 faz, faz 1-2 izle, 3-8 al, 9-10 bekle)
NUM_PHASES = 10
PHASE_DURATION = 30.0
# Volatilite bazli faz butce oranlari
PHASE_BUDGET_LOW_VOL = 0.167   # dusuk vol: %16.7/faz (6 aktif faz x %16.7 = %100)
PHASE_BUDGET_NORMAL = 0.130    # normal: %13/faz (biraz konservatif)
PHASE_BUDGET_HIGH_VOL = 0.08   # yuksek vol: %8/faz (toplam %48, geri kalan korunur)
VOL_LOW_THRESHOLD = 0.05       # BTC <%0.05 degisim = dusuk
VOL_HIGH_THRESHOLD = 0.15      # BTC >%0.15 degisim = yuksek

# Orderbook derinlik filtresi
DEPTH_MAX_RATIO = 0.20  # emir boyutu max orderbook derinliginin %20'si

# Faz rolleri
PHASE_WATCH = {0}              # izle (0-30s)
PHASE_ACTIVE = {1,2,3,4,5,6,7}  # al (30-240s)
PHASE_WAIT = {8, 9}            # bekle (240-300s)


class LimitBotStrategy:
    """%3 garantili kar — spread >= %3 olunca esit cift al."""

    def __init__(self, config: BotConfig):
        self.config = config

        # ClobClient: credentials varsa gerçek, yoksa test mode
        clob_client = _create_clob_client(config)
        test_mode = config.test_mode or (clob_client is None)
        self.order_mgr = OrderManager(clob_client=clob_client, test_mode=test_mode, tick_size=0.01)
        self.portfolio = PaperPortfolio()
        self.limit_engine = LimitEngine(self.order_mgr, self.portfolio)
        self.scanner = MarketScanner()
        self.market_feed: Optional[MarketDataFeed] = None
        self.btc_feed: Optional[BtcPriceFeed] = None
        self._btc_ws_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.dashboard = None

        self.bot_active = False
        self.budget: float = 1000.0
        self._current_asset_ids: list[str] = []
        self._market_end_time: float = 0.0
        self._market_question: str = ""
        self._start_btc_price: float = 0.0  # market basindaki BTC fiyati
        self._ws_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

        self._spent: float = 0.0
        self._auto_mode: bool = True
        self._auto_task: Optional[asyncio.Task] = None
        self._tick_count: int = 0
        self._lock = asyncio.Lock()
        self.signal_tracker: Optional[SignalTracker] = None
        self._signal_state: str = SignalState.NEUTRAL

        # Faz bazli butce
        self._phase_spent: list[float] = [0.0] * NUM_PHASES  # her fazda harcanan
        self._current_phase: int = 0
        self._market_start_ts: float = 0

        # Baslangic referans fiyatlari (sinyal hesabi icin)
        self._start_up_mid: float = 0
        self._start_dn_mid: float = 0

        # IZLE fazi: 60s sinyal toplama
        self._izle_samples: list[float] = []  # btc change_pct ornekleri
        self._izle_signal: str = SignalState.NEUTRAL  # toplanan sinyal
        self._izle_avg: float = 0.0  # ort BTC-PTB farki

        self.process_log: list[str] = []
        self.stats = {"fills": 0, "orders_sent": 0, "total_cycles": 0, "markets_traded": 0}
        if not test_mode:
            self._log("GERCEK TRADE MODU — ClobClient aktif")
        self.market_history: list[dict] = self._load_history()
        self.order_archive: list[dict] = self._load_archive()

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.process_log.append(f"[{ts}] {msg}")
        if len(self.process_log) > 50:
            self.process_log = self.process_log[-50:]
        log.info(msg)

    # ── Persistence ──────────────────────────────────────────────

    def _load_archive(self) -> list[dict]:
        try:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            if os.path.exists(ARCHIVE_FILE):
                with open(ARCHIVE_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_archive(self):
        try:
            with open(ARCHIVE_FILE, "w") as f:
                json.dump(self.order_archive[-200:], f, indent=2)
        except Exception:
            pass

    def _load_history(self) -> list[dict]:
        try:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r") as f:
                    return json.load(f)[-100:]
        except Exception:
            pass
        return []

    def _save_history(self):
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(self.market_history[-100:], f, indent=2)
        except Exception:
            pass

    def _archive_fill(self, token: str, price: float, size: float, cost: float):
        self.order_archive.append({
            "ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "market": self._market_question, "token": token,
            "price": price, "size": round(size, 1), "cost": round(cost, 4),
        })
        self._save_archive()

    # ── Commands ─────────────────────────────────────────────────

    async def handle_command(self, cmd: dict):
        action = cmd.get("action", "")
        if action in ("connect", "start"):
            self.budget = max(10, float(cmd.get("budget", self.budget)))
            if not self.bot_active:
                asyncio.create_task(self._start())
            if not self._auto_mode:
                self._auto_mode = True
                if not self._auto_task:
                    self._auto_task = asyncio.create_task(self._auto_loop())
        elif action in ("disconnect", "stop"):
            self._auto_mode = False
            if self._auto_task:
                self._auto_task.cancel()
                self._auto_task = None
            asyncio.create_task(self._disconnect())
        elif action == "auto":
            self.budget = max(10, float(cmd.get("budget", self.budget)))
            self._auto_mode = not self._auto_mode
            if self._auto_mode:
                self._log("AUTO ON")
                if not self._auto_task:
                    self._auto_task = asyncio.create_task(self._auto_loop())
            else:
                self._log("AUTO OFF")
                if self._auto_task:
                    self._auto_task.cancel()
                    self._auto_task = None
        elif action == "sell":
            asyncio.create_task(self._do_sell(cmd.get("token", "UP")))
        elif action == "cancel":
            await self.order_mgr.cancel_all()
        elif action == "set_budget":
            self.budget = max(10, float(cmd.get("budget", self.budget)))
        elif action == "clear_history":
            self.market_history.clear()
            self._save_history()
            self._log("Gecmis temizlendi")

    # ── Auto ─────────────────────────────────────────────────────

    async def auto_start(self):
        self._log("━━ %3 GARANTILI KAR BOTU ━━")
        self._log(f"Butce: ${self.budget:.0f} | Spread >= %{MIN_PROFIT_PCT:.0f} bekle → esit cift al")
        self._auto_mode = True
        self._auto_task = asyncio.create_task(self._auto_loop())

    async def _auto_loop(self):
        while self._auto_mode:
            try:
                if not self.bot_active:
                    now = int(time.time())
                    current_window = now - (now % 300)
                    mi = self.scanner._fetch_market(current_window)
                    if mi and mi.get("end_time", 0) > now + 30:
                        await self._start()
                    else:
                        next_window = current_window + 300
                        wait = next_window - now
                        self._log(f"[AUTO] Sonraki pencereye {wait}s")
                        while time.time() < next_window - 2 and self._auto_mode:
                            await asyncio.sleep(1)
                        if self._auto_mode and not self.bot_active:
                            await self._start()
                else:
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Auto: %s", e)
                await asyncio.sleep(5)

    # ── Start ────────────────────────────────────────────────────

    async def _start(self):
        if self.bot_active:
            return

        if not self.btc_feed:
            self._log("Binance baglaniyor...")
            self.btc_feed = BtcPriceFeed()
            self._btc_ws_task = asyncio.create_task(self.btc_feed.connect())
            await asyncio.sleep(3)
            if self.btc_feed.current_price > 0:
                self._log(f"BTC: ${self.btc_feed.current_price:,.2f}")

        now = int(time.time())
        current_window = now - (now % 300)
        mi = self.scanner._fetch_market(current_window) or self.scanner._fetch_market(current_window + 300)
        if not mi:
            self._log("HATA: Market bulunamadi!")
            return

        self._current_asset_ids = [mi["up_token_id"], mi["down_token_id"]]
        self._market_end_time = mi.get("end_time", 0)
        self._market_question = mi.get("question", "")
        self.order_mgr.tick_size = mi.get("tick_size", 0.01)

        # Market basindaki BTC fiyati = referans
        self._start_btc_price = self.btc_feed.current_price if self.btc_feed else 0

        self._log(f"Market: {self._market_question}")
        self._log(f"BTC baslangic: ${self._start_btc_price:,.2f}")

        self.portfolio = PaperPortfolio()
        self.portfolio.budget = self.budget
        self.limit_engine.portfolio = self.portfolio
        self.order_mgr.active_orders.clear()
        self._spent = 0.0
        self._tick_count = 0

        self.market_feed = MarketDataFeed(
            ws_url=self.config.polymarket.ws_market_url,
            asset_ids=self._current_asset_ids,
        )
        self.market_feed.on_trade(self._on_trade)
        book_getter = lambda aid: self.market_feed.get_book(aid) if self.market_feed else None
        self.limit_engine.set_book_getter(book_getter)

        # Signal tracker baslat (200ms = 5x/s, 10 faz x 30s)
        self.signal_tracker = SignalTracker(book_getter)
        self.signal_tracker.configure(self._current_asset_ids[0], self._current_asset_ids[1])
        self.signal_tracker.on_signal(self._on_signal_change)
        self.signal_tracker.on_hedge(self._on_hedge_request)
        self._signal_state = SignalState.NEUTRAL

        self.bot_active = True
        self._ws_task = asyncio.create_task(self.market_feed.connect())
        asyncio.create_task(self.signal_tracker.start())
        self._monitor_task = asyncio.create_task(self._main_loop())
        # Heartbeat: production modda emirleri canlı tutar
        if not self.order_mgr.test_mode and not self._heartbeat_task:
            self._heartbeat_task = asyncio.create_task(
                self.order_mgr.start_heartbeat(self.config.risk.heartbeat_interval_sec))
        self.stats["markets_traded"] += 1

    async def _disconnect(self):
        if not self.bot_active:
            return
        self.bot_active = False
        if self.signal_tracker:
            await self.signal_tracker.stop()
            self.signal_tracker = None
        self.limit_engine.cancel_active()
        await self.order_mgr.cancel_all()
        self.order_mgr.stop_heartbeat()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._ws_task:
            self._ws_task.cancel()
        if self._btc_ws_task:
            self._btc_ws_task.cancel()
            self._btc_ws_task = None
            self.btc_feed = None
        if self.market_feed:
            await self.market_feed.disconnect()
            self.market_feed = None
        self._log("Durduruldu.")

    # ── Signal callback ───────────────────────────────────────────

    def _on_signal_change(self, new_state: str, snapshot):
        """SignalTracker sinyal degistirdiginde cagirilir."""
        old = self._signal_state
        self._signal_state = new_state
        if new_state == SignalState.NEUTRAL:
            self._log(f"SINYAL NEUTRAL | makas%{snapshot.spread_pct:.1f}")
        else:
            direction = "UP" if new_state == SignalState.STRONG_UP else "DOWN"
            self._log(
                f"SINYAL {direction} | UP¢{snapshot.up_mid*100:.0f} DN¢{snapshot.dn_mid*100:.0f} "
                f"makas%{snapshot.spread_pct:.1f} guven%{snapshot.confidence*100:.0f}"
            )

    # ── Hedge callback ─────────────────────────────────────────────

    def _on_hedge_request(self, req: HedgeRequest):
        """SignalTracker risk hedge talebi gonderdiginde cagirilir."""
        if not self.bot_active or not self.market_feed:
            return
        idx = 0 if req.side == "UP" else 1
        remaining = max(0, self.budget - self.portfolio.total_cost() - self.order_mgr.pending_notional())
        hedge_budget = min(req.max_cost, remaining)
        if hedge_budget < 1.0 or req.price <= 0:
            return
        size = round(hedge_budget / req.price, 1)
        if size < 5.0:
            return
        # Senkron callback icinde async emir koyamayiz — task olustur
        asyncio.create_task(self._place_hedge(idx, req.side, req.price, size, hedge_budget))

    async def _place_hedge(self, idx: int, side: str, price: float, size: float, budget: float):
        """Hedge emrini yerlestirir."""
        order = await self.order_mgr.place_limit_order(
            asset_id=self._current_asset_ids[idx],
            side=OrderSide.BUY, price=price, size=size, expiration_sec=90)
        if order:
            self.stats["orders_sent"] += 1
            self._log(f"HEDGE {side} | {size:.0f}sh @¢{price*100:.0f} = ${budget:.2f}")

    # ── Main Loop: 10 faz x 30s, her faz $UP + $DN esit taker ──

    async def _main_loop(self):
        """300 saniye = 10 faz x 30 saniye.
        Her faz: butcenin %10'u = $100 → $50 UP + $50 DN taker emir.
        Sinyal varsa max %5 kayma (52.50/47.50).
        Faz bitene kadar emir gonderilmez — 30 saniye bekle.
        """
        # Orderbook hazir olana kadar bekle
        for _ in range(20):
            ub = self.market_feed.get_book(self._current_asset_ids[0]) if self.market_feed else None
            db = self.market_feed.get_book(self._current_asset_ids[1]) if self.market_feed else None
            if ub and ub.bids and db and db.bids:
                break
            await asyncio.sleep(0.5)

        # Referans fiyatlar
        ub = self.market_feed.get_book(self._current_asset_ids[0]) if self.market_feed else None
        db = self.market_feed.get_book(self._current_asset_ids[1]) if self.market_feed else None
        self._start_up_mid = ub.mid_price if ub and ub.mid_price else 0.50
        self._start_dn_mid = db.mid_price if db and db.mid_price else 0.50
        self._market_start_ts = time.time()
        self._phase_spent = [0.0] * NUM_PHASES
        self._current_phase = -1

        self._izle_samples = []
        self._izle_signal = SignalState.NEUTRAL
        self._izle_avg = 0.0

        self._log(
            f"BASLA | ref UP¢{self._start_up_mid*100:.0f} DN¢{self._start_dn_mid*100:.0f} | "
            f"${self.budget:.0f} | F1-2:IZLE(60s sinyal topla) F3-8:AL F9-10:BEKLE"
        )

        while self.bot_active:
            try:
                now = time.time()
                time_left = max(0, self._market_end_time - now) if self._market_end_time else 999
                self._tick_count += 1
                self.order_mgr.expire_stale_orders()

                if time_left <= 5:
                    await self._resolve_market()
                    return

                # ── Faz hesabi ──
                elapsed = now - self._market_start_ts
                phase = min(NUM_PHASES - 1, int(elapsed / PHASE_DURATION))

                # ── Risk kontrolu: her tick ──
                if self._check_max_loss():
                    # Zarar %10'u gecti → bekleyen emirleri iptal et, yeni emir gonderme
                    await self.order_mgr.cancel_all()
                    await asyncio.sleep(1)
                    continue

                # ── IZLE fazinda sinyal topla ──
                if phase in PHASE_WATCH:
                    if self.btc_feed and self.btc_feed.current_price > 0:
                        self._izle_samples.append(self.btc_feed.change_pct)

                # ── Yeni faz basladi ──
                if phase != self._current_phase:
                    old_phase = self._current_phase
                    self._current_phase = phase

                    if phase in PHASE_WATCH:
                        self._log(f"FAZ {phase+1}/{NUM_PHASES} IZLE | sinyal toplaniyor | {int(time_left)}s")

                    elif phase in PHASE_WAIT:
                        self._log(f"FAZ {phase+1}/{NUM_PHASES} BEKLE | sonuc bekleniyor | {int(time_left)}s")

                    else:
                        # Ilk AL fazina geciste IZLE verisini kilitle
                        if old_phase in PHASE_WATCH or (old_phase == -1 and phase in PHASE_ACTIVE):
                            self._lock_izle_signal()
                        await self._execute_phase(phase, time_left)

                # Portfolio guncelle (signal tracker icin)
                up_sh = self.portfolio.up.shares
                dn_sh = self.portfolio.down.shares
                if self.signal_tracker:
                    self.signal_tracker.update_portfolio(
                        up_sh, dn_sh,
                        self.portfolio.up.total_cost, self.portfolio.down.total_cost)

                # Periyodik log (her 5 saniye)
                if self._tick_count % 5 == 0:
                    paired = min(up_sh, dn_sh)
                    up_book = self.market_feed.get_book(self._current_asset_ids[0]) if self.market_feed else None
                    dn_book = self.market_feed.get_book(self._current_asset_ids[1]) if self.market_feed else None
                    up_ask = (up_book.best_ask or 0) if up_book else 0
                    dn_ask = (dn_book.best_ask or 0) if dn_book else 0
                    sig_info = self.signal_tracker.current() if self.signal_tracker else {}
                    self._log(
                        f"[F{phase+1}/{NUM_PHASES}] "
                        f"UP¢{up_ask*100:.0f} DN¢{dn_ask*100:.0f} | "
                        f"YKR{up_sh:.0f} ASG{dn_sh:.0f} Cift:{paired:.0f} | "
                        f"${self.portfolio.total_cost():.0f}/{self.budget:.0f} | "
                        f"makas%{sig_info.get('spread_pct',0):.0f} | {int(time_left)}s"
                    )

                self.stats["total_cycles"] += 1

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Loop: %s", e)

            await asyncio.sleep(1)

    # ── Risk kontrolu: max %10 zarar ────────────────────────────

    def _check_max_loss(self) -> bool:
        """Worst-case zarar %10'u gecerse True doner → emir gonderme.

        Hesap:
          UP kazanirsa: payout = UP_shares × $1
          DOWN kazanirsa: payout = DN_shares × $1
          worst_pnl = min(UP payout, DN payout) - total_cost
          zarar% = |worst_pnl| / budget × 100

        Ornek: $1000 butce, $100 harcandi
          UP: 80sh, DN: 20sh → UP kazanir: $80-$100=-$20, DN kazanir: $20-$100=-$80
          worst = -$80, zarar% = 80/1000 = %8 → OK
          worst = -$110 olursa, %11 > %10 → DURDUR
        """
        cost = self.portfolio.total_cost()
        if cost <= 0:
            return False

        up_sh = self.portfolio.up.shares
        dn_sh = self.portfolio.down.shares

        # Her iki senaryo
        up_wins_pnl = up_sh - cost     # UP kazanirsa
        dn_wins_pnl = dn_sh - cost     # DOWN kazanirsa
        worst_pnl = min(up_wins_pnl, dn_wins_pnl)

        if worst_pnl >= 0:
            return False  # Her iki senaryoda da kar — sorun yok

        loss_pct = abs(worst_pnl) / self.budget * 100

        if loss_pct >= MAX_LOSS_PCT:
            self._log(
                f"RISK DURDUR | zarar%{loss_pct:.1f} >= %{MAX_LOSS_PCT:.0f} | "
                f"worst:${worst_pnl:+.0f} cost:${cost:.0f} | "
                f"UP:{up_sh:.0f}sh DN:{dn_sh:.0f}sh"
            )
            return True
        return False

    # ── Faz emirleri ────────────────────────────────────────────

    def _phase_budget_pct(self) -> float:
        """Volatiliteye gore faz butce orani."""
        if not self.btc_feed:
            return PHASE_BUDGET_NORMAL
        vol = abs(self.btc_feed.change_pct)
        if vol < VOL_LOW_THRESHOLD:
            return PHASE_BUDGET_LOW_VOL
        elif vol > VOL_HIGH_THRESHOLD:
            return PHASE_BUDGET_HIGH_VOL
        return PHASE_BUDGET_NORMAL

    def _orderbook_depth(self, book) -> float:
        """Top 5 bid seviyesindeki toplam share sayisi."""
        if not book or not book.bids:
            return 0.0
        return sum(level.size for level in book.bids[:5])

    def _worst_case_after(self, extra_up_cost: float, extra_up_sh: float,
                          extra_dn_cost: float, extra_dn_sh: float) -> float:
        """Simdi + planlanan alim sonrasi worst-case zarar yuzdesini hesapla."""
        cost = self.portfolio.total_cost() + extra_up_cost + extra_dn_cost
        if cost <= 0:
            return 0.0
        up_sh = self.portfolio.up.shares + extra_up_sh
        dn_sh = self.portfolio.down.shares + extra_dn_sh
        worst_pnl = min(up_sh - cost, dn_sh - cost)
        if worst_pnl >= 0:
            return 0.0
        return abs(worst_pnl) / self.budget * 100

    async def _execute_phase(self, phase: int, time_left: float):
        """Aktif faz: ESIT SHARE al. Sinyal yok, skew yok, basit.

        Matematik:
          N share UP @¢P1 + N share DN @¢P2 = N × (P1+P2) maliyet
          UP kazanirsa: N × $1 payout
          DN kazanirsa: N × $1 payout
          Kar = N × (1 - P1 - P2) = N × spread → GARANTI

        Tek kural: pair cost < $1 oldugu surece esit share al.
        UP veya DN hangisi gelirse gelsin ayni kar.
        """
        # Volatilite bazli faz butcesi
        budget_pct = self._phase_budget_pct()
        phase_budget = self.budget * budget_pct
        remaining_total = max(0, self.budget - self.portfolio.total_cost() - self.order_mgr.pending_notional())
        available = min(phase_budget, remaining_total)
        vol_label = "LOW" if budget_pct >= PHASE_BUDGET_LOW_VOL else "HIGH" if budget_pct <= PHASE_BUDGET_HIGH_VOL else "MID"

        if available < 5.0:
            self._log(f"FAZ {phase+1} AL | butce yetersiz ${available:.0f}")
            return

        # Orderbook
        up_book = self.market_feed.get_book(self._current_asset_ids[0]) if self.market_feed else None
        dn_book = self.market_feed.get_book(self._current_asset_ids[1]) if self.market_feed else None
        up_bid = (up_book.best_bid or 0) if up_book else 0
        dn_bid = (dn_book.best_bid or 0) if dn_book else 0

        if up_bid <= 0 or dn_bid <= 0:
            self._log(f"FAZ {phase+1} AL | bid fiyat yok")
            return

        # Fiyat korumasi: UP veya DN ¢89+ ise alim yapma
        if up_bid >= 0.89 or dn_bid >= 0.89:
            self._log(f"FAZ {phase+1} AL | ATLA ¢{max(up_bid,dn_bid)*100:.0f} >= ¢89")
            return

        pair_cost = up_bid + dn_bid
        if pair_cost >= MAX_PAIR_COST:
            self._log(f"FAZ {phase+1} AL | ATLA pair ¢{pair_cost*100:.0f} >= ¢100")
            return

        spread = 1.0 - pair_cost
        spread_pct = spread / pair_cost * 100 if pair_cost > 0 else 0

        # Derinlik filtresi
        up_depth = self._orderbook_depth(up_book)
        dn_depth = self._orderbook_depth(dn_book)

        # ── ESIT SHARE hesabi ──
        # N share = available / pair_cost
        target_shares = available / pair_cost
        up_cost = target_shares * up_bid
        dn_cost = target_shares * dn_bid

        # Derinlik limiti: share sayisini likiditie sinirla
        max_shares_by_depth = min(
            up_depth * DEPTH_MAX_RATIO if up_depth > 0 else 999,
            dn_depth * DEPTH_MAX_RATIO if dn_depth > 0 else 999,
        )
        if target_shares > max_shares_by_depth and max_shares_by_depth > 5:
            target_shares = max_shares_by_depth
            up_cost = target_shares * up_bid
            dn_cost = target_shares * dn_bid

        # Risk on-kontrol
        projected_loss = self._worst_case_after(up_cost, target_shares, dn_cost, target_shares)
        if projected_loss >= MAX_LOSS_PCT:
            scale = (MAX_LOSS_PCT - 1) / max(projected_loss, 0.01)
            target_shares *= scale
            up_cost = target_shares * up_bid
            dn_cost = target_shares * dn_bid

        # ESIT SHARE emir koy — her iki tarafa ayni share sayisi
        up_placed = await self._maker_buy("UP", 0, up_bid, up_cost)
        dn_placed = await self._maker_buy("DOWN", 1, dn_bid, dn_cost)
        self._phase_spent[phase] = up_placed + dn_placed

        guaranteed_profit = target_shares * spread
        self._log(
            f"FAZ {phase+1}/{NUM_PHASES} AL | {target_shares:.0f}sh x2 | vol:{vol_label} | "
            f"UP ${up_placed:.0f}@¢{up_bid*100:.0f} DN ${dn_placed:.0f}@¢{dn_bid*100:.0f} | "
            f"pair¢{pair_cost*100:.0f} spread¢{spread*100:.1f}(%{spread_pct:.1f}) | "
            f"gar.kar:${guaranteed_profit:.1f} | risk%{self._worst_case_after(0,0,0,0):.1f} | {int(time_left)}s"
        )

    async def _maker_buy(self, side: str, idx: int, bid_price: float, budget: float) -> float:
        """Orderbook bid seviyelerine dagitarak maker BUY emirleri koy.

        ¢89+ fiyattan emir GONDERMEZ.
        """
        if bid_price >= 0.89:
            return 0.0
        if budget < 2.5 or bid_price <= 0:
            return 0.0

        book = self.market_feed.get_book(self._current_asset_ids[idx]) if self.market_feed else None
        if not book or not book.bids:
            return 0.0

        # Top 5 bid seviyesine dagit
        levels = book.bids[:5]
        per_level = budget / len(levels)
        total_placed = 0.0

        for level in levels:
            price = level.price
            if price <= 0 or price >= 0.89:
                continue

            remaining_budget = budget - total_placed
            level_budget = min(per_level, remaining_budget)
            if level_budget < 2.0:
                break

            size = round(level_budget / price, 1)
            if size < 5.0:
                continue
            cost = size * price
            if cost > remaining_budget:
                size = round(remaining_budget / price, 1)
                if size < 5.0:
                    continue
                cost = size * price

            order = await self.order_mgr.place_limit_order(
                asset_id=self._current_asset_ids[idx],
                side=OrderSide.BUY, price=price, size=size,
                expiration_sec=90, taker=False)
            if order:
                total_placed += cost
                self.stats["orders_sent"] += 1

        return total_placed

    async def _taker_buy(self, side: str, idx: int, ask_price: float, budget: float) -> float:
        """Best ask'tan taker BUY emri gonder. Harcanan tutari dondurur."""
        if budget < 2.5 or ask_price <= 0 or ask_price >= 1.0:
            return 0.0

        size = round(budget / ask_price, 1)
        if size < 5.0:
            return 0.0
        cost = size * ask_price
        if cost > budget:
            size = round(budget / ask_price, 1)
            if size < 5.0:
                return 0.0
            cost = size * ask_price

        order = await self.order_mgr.place_limit_order(
            asset_id=self._current_asset_ids[idx],
            side=OrderSide.BUY, price=ask_price, size=size,
            expiration_sec=90, taker=True)

        if order:
            # Taker emir aninda dolar — portfolio'ya kaydet
            btc = self.btc_feed.current_price if self.btc_feed else 0
            ledger = self.portfolio.up if side == "UP" else self.portfolio.down
            ledger.record_buy(ask_price, size, btc)
            self._archive_fill(side, ask_price, size, cost)
            self.stats["orders_sent"] += 1
            self.stats["fills"] += 1
            return cost
        return 0.0

    # ── Sinyal: start noktasindan sapma ───────────────────────────

    def _lock_izle_signal(self):
        """IZLE fazinda toplanan 60s sinyal verisini kilitle.

        60 saniye boyunca toplanan BTC change_pct orneklerinin ortalamasini alir.
        Bu ortalama yon tayini icin kullanilir — anlik dalgalanmadan etkilenmez.
        """
        if not self._izle_samples:
            self._izle_signal = SignalState.NEUTRAL
            self._izle_avg = 0.0
            self._log("IZLE SONUC | veri yok → NEUTRAL")
            return

        avg = sum(self._izle_samples) / len(self._izle_samples)
        self._izle_avg = avg

        # Ortalama BTC-PTB farki yorumlama
        # avg > +0.03 → BTC PTB'den yukarda → UP kazanir
        # avg < -0.03 → BTC PTB'den asagida → DOWN kazanir
        if avg > 0.03:
            self._izle_signal = SignalState.STRONG_UP
        elif avg < -0.03:
            self._izle_signal = SignalState.STRONG_DOWN
        else:
            self._izle_signal = SignalState.NEUTRAL

        direction = "UP" if self._izle_signal == SignalState.STRONG_UP else \
                    "DN" if self._izle_signal == SignalState.STRONG_DOWN else "—"
        self._log(
            f"IZLE SONUC | {len(self._izle_samples)} ornek | "
            f"ort BTC-PTB: {avg:+.4f}% | YON: {direction}"
        )

    def _compute_signal_from_start(self, up_mid: float, dn_mid: float) -> str:
        """BTC baslangic fiyatindan farka gore sinyal.

        BTC current > start → yukseliyor → UP
        BTC current < start → dususyor → DOWN
        """
        if not self.btc_feed or self.btc_feed.current_price <= 0 or self._start_btc_price <= 0:
            return SignalState.NEUTRAL

        diff_pct = (self.btc_feed.current_price - self._start_btc_price) / self._start_btc_price * 100

        if diff_pct > 0.05:
            return SignalState.STRONG_UP
        elif diff_pct < -0.05:
            return SignalState.STRONG_DOWN
        return SignalState.NEUTRAL

    # ── Dagitim: max %5 kayma ─────────────────────────────────────

    def _compute_allocation(self, signal: str, up_price: float, dn_price: float) -> tuple[float, float]:
        """Sinyal bazli butce dagitimi.

        NEUTRAL: esit share (fiyat oranina gore)
        STRONG_UP: butcenin %90'i UP'a, %10 DN'a (hedge)
        STRONG_DOWN: butcenin %90'i DN'a, %10 UP'a (hedge)

        Sinyal yonu gosteriyorsa o yone agirlikli al.
        Zayif tarafa min %10 hedge — tamamen sifirlamak yerine ucuz hedge.

        Ornek (STRONG_DOWN, UP ¢10, DN ¢90):
          UP'a %10 = $10 → 100 share (ucuz hedge)
          DN'a %90 = $90 → 100 share (guclu taraf)
          DN kazanirsa: payout $100, cost $100, kar $0 + spread
          UP kazanirsa: payout $100 (hedge), cost $100, kayip minimal
        """
        total = up_price + dn_price
        if total <= 0:
            return 0.5, 0.5

        if signal == SignalState.NEUTRAL:
            # Esit share: fiyat oranina gore
            base_up = up_price / total
            return base_up, 1.0 - base_up

        # BTC direction'dan confidence al (0→1)
        btc_dir = abs(self.btc_feed.direction) if self.btc_feed else 0.5
        confidence = min(1.0, btc_dir)

        # Confidence 0.3→ %70 guclu, 1.0→ %80 guclu (max %80, min %20 hedge)
        strong_ratio = min(1.0 - HEDGE_MIN_RATIO, 0.60 + confidence * 0.20)
        weak_ratio = max(HEDGE_MIN_RATIO, 1.0 - strong_ratio)
        strong_ratio = 1.0 - weak_ratio

        if signal == SignalState.STRONG_UP:
            return strong_ratio, weak_ratio
        else:  # STRONG_DOWN
            return weak_ratio, strong_ratio

    # ── Buy Side (hedge icin kullanilir) ─────────────────────────

    async def _buy_side(self, side: str, budget: float, up_bid: float, dn_bid: float):
        idx = 0 if side == "UP" else 1
        bid = up_bid if side == "UP" else dn_bid
        await self._maker_buy(side, idx, bid, budget)

    # ── Market Resolve ───────────────────────────────────────────

    async def _resolve_market(self):
        self._log("Market kapaniyor...")

        up_sh = self.portfolio.up.shares
        dn_sh = self.portfolio.down.shares
        paired = min(up_sh, dn_sh)

        btc_close = self.btc_feed.current_price if self.btc_feed else 0
        start_price = self._start_btc_price
        if btc_close > 0 and start_price > 0:
            winner = "UP" if btc_close >= start_price else "DOWN"
        else:
            up_p, dn_p = self._get_prices()
            winner = "UP" if up_p >= dn_p else "DOWN"

        if self.portfolio.total_shares() > 0:
            res = self.portfolio.resolve_market(winner)
            pnl = res["resolved_pnl"]
            pnl_pct = (pnl / res["total_cost"] * 100) if res["total_cost"] > 0 else 0
            w_tr = "YUKARI" if winner == "UP" else "ASAGI"

            self._log(f"━━ {w_tr} KAZANDI ━━")
            self._log(
                f"Cift:{paired:.0f} | Odeme:${res['payout']:.2f} | "
                f"Maliyet:${res['total_cost']:.2f} | K/Z:${pnl:+.2f} (%{pnl_pct:+.1f})"
            )

            snapshot = self.portfolio.snapshot_for_history(
                question=self._market_question, winner=winner,
                btc_close=btc_close, ptb=start_price,
            )
            snapshot["ticks"] = self._tick_count
            snapshot["paired"] = paired
            self.market_history.append(snapshot)
            self._save_history()

            cumulative = sum(h.get("resolved_pnl", 0) for h in self.market_history)
            total = len(self.market_history)
            wins = sum(1 for h in self.market_history if h.get("resolved_pnl", 0) > 0)
            self._log(f"#{total} | Bu:${pnl:+.2f} | Toplam:${cumulative:+.2f} | Kazanma:{wins}/{total}")
        else:
            self._log("Islem yok — %3 spread bulunamadi")

        self.bot_active = False
        await self.order_mgr.cancel_all()
        if self._ws_task:
            self._ws_task.cancel()
        if self.market_feed:
            await self.market_feed.disconnect()
            self.market_feed = None

    # ── Sell (devre disi — buy-only bot) ──────────────────────────

    async def _do_sell(self, token: str):
        """Buy-only bot: satis devre disi."""
        self._log(f"SATIS {token}: devre disi (buy-only bot)")

    # ── Fill tracking ────────────────────────────────────────────

    def _on_trade(self, asset_id: str, trade: LastTrade):
        # Senkron callback — portfolio ve order state'i atomik guncelle
        if not self.bot_active:
            return
        is_up = (asset_id == self._current_asset_ids[0]) if self._current_asset_ids else True
        ledger = self.portfolio.up if is_up else self.portfolio.down
        btc = self.btc_feed.current_price if self.btc_feed else 0
        tok = "UP" if is_up else "DOWN"

        # Kalan trade boyutunu emirlere sırayla dağıt
        remaining = trade.size

        # Fiyat önceliğine göre sırala: BUY → yüksek fiyat önce, SELL → düşük fiyat önce
        orders = self.order_mgr.get_live_orders(asset_id)
        buys = sorted([o for o in orders if o.side == OrderSide.BUY], key=lambda o: o.price, reverse=True)
        sells = sorted([o for o in orders if o.side == OrderSide.SELL], key=lambda o: o.price)
        orders = buys + sells

        for order in orders:
            if remaining <= 0:
                break

            # Trade side = taker yonu: "BUY" trade → resting SELL emirleri doldu
            #                         "SELL" trade → resting BUY emirleri doldu
            # Karsi taraf eslesmesi: trade side ile ayni yondeki emirler doldurulmaz
            if trade.side:
                taker_side = trade.side.upper()
                if taker_side == "BUY" and order.side == OrderSide.BUY:
                    continue  # BUY trade sadece SELL emirlerini doldurur
                if taker_side == "SELL" and order.side == OrderSide.SELL:
                    continue  # SELL trade sadece BUY emirlerini doldurur

            price_match = False
            if order.side == OrderSide.BUY and trade.price <= order.price:
                price_match = True
            elif order.side == OrderSide.SELL and trade.price >= order.price:
                price_match = True

            if not price_match:
                continue

            # Emirden kalan dolmamış miktar
            unfilled = order.size - order.filled_size
            if unfilled <= 0:
                continue

            # Trade'den bu emre düşen miktar
            fill_qty = min(remaining, unfilled)
            remaining -= fill_qty
            order.filled_size += fill_qty

            # Buy-only bot: sadece BUY emirleri doldurulur
            if order.side != OrderSide.BUY:
                continue

            ledger.record_buy(order.price, fill_qty, btc)
            self._archive_fill(tok, order.price, fill_qty, order.price * fill_qty)
            t = "YKR" if is_up else "ASG"
            self._log(f"DOLUM {t} {fill_qty:.0f}sh @¢{order.price*100:.0f}")

            self.stats["fills"] += 1

            # Emir tamamen doldu mu?
            if order.filled_size >= order.size:
                order.status = "MATCHED"
                self.order_mgr.active_orders.pop(order.order_id, None)

    def _get_prices(self) -> tuple[float, float]:
        up_p = dn_p = 0.0
        if self.market_feed and self._current_asset_ids:
            ub = self.market_feed.get_book(self._current_asset_ids[0])
            if ub and ub.mid_price:
                up_p = ub.mid_price
            if len(self._current_asset_ids) > 1:
                db = self.market_feed.get_book(self._current_asset_ids[1])
                if db and db.mid_price:
                    dn_p = db.mid_price
        return up_p, dn_p
