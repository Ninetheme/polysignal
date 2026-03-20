"""PolySignal — guclu yone odaklanan directional takip stratejisi.

Kural:
  1. Guclu sinyal gorulunce sadece o yone alim yap.
  2. Orderbook'ta ¢90 gorulurse yeni alim durur.
  3. Yon tersine donup onaylaninca, yeni guclu yone eski guclu yone
     harcanan miktar kadar catch-up yatirimi yap.
  4. En kotu senaryodaki zarar toplam butcenin %10'unu gecemez.
"""

import asyncio
import json
import os
import time
from typing import Optional

from config.settings import BotConfig
from core.market_data import MarketDataFeed, OrderBook, LastTrade
from core.market_scanner import MarketScanner
from core.order_manager import OrderManager, OrderSide
from core.paper_engine import PaperPortfolio
from core.limit_engine import LimitEngine
from core.signal_tracker import SignalTracker, SignalState, HedgeRequest
from core.ai_agent import AITradingAgent
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

# Strateji — Directional Follow + Reversal Catch-Up
MAX_BUDGET_RISK_PCT = 10.0      # toplam butcenin en fazla %10'u riskte
IZLE_DURATION = 30.0            # ilk 30s izleme
MIN_IZLE_BEFORE_ENTRY = 5.0     # guclu yon erken netlesirse erken gir
STOP_BEFORE_END = 10.0          # son 10s alim durdur
BUDGET_PER_SEC = 0.004          # butcenin %0.4'u/saniye (250s = %100)
DIRECTION_CONFIRM_SECS = 3      # yon degisimi onaylamak icin 3s ardisik
NUM_PHASES = 10                 # dashboard uyumlulugu icin
PHASE_DURATION = 30.0
ORDERBOOK_STOP_PRICE = 0.90     # orderbook 90 gorduyse yeni alim yok
CHEAP_TAKE_PRICE = 0.15         # ¢15 alti ucuz token firsati
CHEAP_TAKE_BUDGET_PCT = 5.0     # toplam butcenin %5'i kadar taker alim
MIN_TRADE_BUDGET = 5.0
MIN_EFFECTIVE_BUDGET = 4.95
MIN_MARKET_TIME_LEFT = 20.0
PHASE_BUDGET_BASE_PCT = 0.03    # her fazda en az butcenin %3'u kullanilabilir
PHASE_BUDGET_MAX_PCT = 0.16     # cok guclu sinyalde faz limiti butcenin %16'sina kadar cikar
PHASE_RELEASE_BASE = 0.25       # zayif sinyalde faz limitinin %25'i kadar tek tick alim
PHASE_RELEASE_MAX = 0.80        # cok guclu sinyalde faz limitinin %80'i kadar tek tick alim


class LimitBotStrategy:
    """Guclu yonde al, reversal'da karsi tarafi butceyi kurtaracak kadar tamamla."""

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
        self.btc_feed = None
        self._btc_ws_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.dashboard = None

        self.bot_active = False
        self.budget: float = 1000.0
        self._current_asset_ids: list[str] = []
        self._market_end_time: float = 0.0
        self._market_question: str = ""
        self._market_budget: float = 0.0  # market acilisindaki butce snapshot'i
        self._start_btc_price: float = 0.0  # legacy alan — orderbook-only modda 0 kalir
        self._ws_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

        self._spent: float = 0.0
        self._auto_mode: bool = True
        self._auto_task: Optional[asyncio.Task] = None
        self._tick_count: int = 0
        self._lock = None
        self.signal_tracker: Optional[SignalTracker] = None
        self._signal_state: str = SignalState.NEUTRAL
        self._risk_sell_triggered: bool = False
        self._cost_rebalance_triggered: bool = False
        self._cost_block_side: Optional[str] = None  # risk nedeniyle gecici bloklu taraf
        self._reversal_target_side: Optional[str] = None
        self._reversal_target_cost: float = 0.0
        self._orderbook_stop_hit: bool = False
        self._orderbook_stop_side: Optional[str] = None
        self._cheap_take_fired: set[str] = set()

        # Faz bazli butce
        self._phase_spent: list[float] = [0.0] * NUM_PHASES  # her fazda harcanan
        self._current_phase: int = 0
        self._market_start_ts: float = 0

        # Baslangic referans fiyatlari (sinyal hesabi icin)
        self._start_up_mid: float = 0
        self._start_dn_mid: float = 0

        # IZLE fazi: 30s sinyal toplama
        self._izle_samples: list[float] = []  # orderbook yon skoru ornekleri (-1..+1)
        self._izle_signal: str = SignalState.NEUTRAL  # toplanan sinyal
        self._izle_avg: float = 0.0  # ort orderbook yon skoru

        # Referans yon takibi (log/PTB gozlemi icin)
        self._active_direction: Optional[str] = None  # "UP" veya "DOWN"
        self._direction_since: float = 0  # bu yonde ne zamandir
        self._direction_changes: int = 0  # kac kez yon degisti
        self._pending_direction: Optional[str] = None  # onay bekleyen yon
        self._pending_since: float = 0  # onay bekleme baslangici
        self._strategy_state: str = "IZLE"  # IZLE / ALIM / BEKLE

        # AI Agent — tam yetki
        self.ai_agent = AITradingAgent()
        self._ai_last_action: str = "HOLD"
        self._ai_reasoning: str = ""

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

    def _spawn_task(self, coro):
        """Create a task only when a running loop exists."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        return loop.create_task(coro)

    def _budget_cap(self) -> float:
        return self.budget

    def _remaining_budget(self) -> float:
        cap = self._budget_cap()
        return max(0.0, cap - self.portfolio.total_cost() - self.order_mgr.pending_notional())

    async def _set_budget_value(self, raw_budget: float, source: str = "UI"):
        old_budget = self.budget
        self.budget = max(10, float(raw_budget))
        self.portfolio.budget = self.budget

        if abs(self.budget - old_budget) < 1e-9:
            return

        if not self.bot_active:
            self._log(f"BUTCE {source} | ${old_budget:.0f} → ${self.budget:.0f}")
            return

        spent = self.portfolio.total_cost()
        pending = self.order_mgr.pending_notional()
        if spent + pending > self.budget and pending > 0:
            cancelled = await self.order_mgr.cancel_all()
            self._log(
                f"BUTCE {source} | ${old_budget:.0f} → ${self.budget:.0f} | "
                f"UP+DOWN+pending tavani asti — {cancelled} emir iptal"
            )
        elif spent > self.budget:
            self._log(
                f"BUTCE {source} | ${old_budget:.0f} → ${self.budget:.0f} | "
                f"acik pozisyon zaten ${spent:.2f}"
            )
        else:
            self._log(
                f"BUTCE {source} | ${old_budget:.0f} → ${self.budget:.0f} | "
                f"kalan:${self._remaining_budget():.2f}"
            )

    # ── Persistence ──────────────────────────────────────────────

    def _load_archive(self) -> list[dict]:
        try:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            if os.path.exists(ARCHIVE_FILE):
                with open(ARCHIVE_FILE, "r") as f:
                    return json.load(f)
        except Exception as e:
            log.error("Archive yukleme hatasi: %s", e)
        return []

    def _save_archive(self):
        try:
            with open(ARCHIVE_FILE, "w") as f:
                json.dump(self.order_archive[-200:], f, indent=2)
        except Exception as e:
            log.error("Archive kayit hatasi: %s", e)

    def _load_history(self) -> list[dict]:
        try:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r") as f:
                    return json.load(f)[-100:]
        except Exception as e:
            log.error("History yukleme hatasi: %s", e)
        return []

    def _save_history(self):
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(self.market_history[-100:], f, indent=2)
        except Exception as e:
            log.error("History kayit hatasi: %s", e)

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
            await self._set_budget_value(cmd.get("budget", self.budget), source="START")
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
            await self._set_budget_value(cmd.get("budget", self.budget), source="AUTO")
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
            await self._set_budget_value(cmd.get("budget", self.budget), source="MANUAL")
        elif action == "clear_history":
            self.market_history.clear()
            self._save_history()
            self._log("Gecmis temizlendi")

    # ── Auto ─────────────────────────────────────────────────────

    async def auto_start(self):
        self._log("━━ DIRECTIONAL FOLLOW BOTU ━━")
        self._log(
            f"Butce: ${self.budget:.0f} | Max risk: %{MAX_BUDGET_RISK_PCT:.0f} | "
            "guclu yone takip, reversal'da catch-up"
        )
        self._auto_mode = True
        self._auto_task = asyncio.create_task(self._auto_loop())

    async def _auto_loop(self):
        while self._auto_mode:
            try:
                if not self.bot_active:
                    now = int(time.time())
                    current_window = now - (now % 300)
                    mi = self.scanner._fetch_market(current_window)
                    if mi and mi.get("end_time", 0) > now + MIN_MARKET_TIME_LEFT:
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

        now = int(time.time())
        current_window = now - (now % 300)
        mi = self.scanner._fetch_market(current_window) or self.scanner._fetch_market(current_window + 300)
        if not mi:
            self._log("HATA: Market bulunamadi!")
            return

        time_left = float(mi.get("end_time", 0) or 0) - time.time()
        if time_left < MIN_MARKET_TIME_LEFT:
            next_market = self.scanner._fetch_market(current_window + 300)
            next_time_left = float(next_market.get("end_time", 0) or 0) - time.time() if next_market else 0.0
            if next_market and next_time_left >= MIN_MARKET_TIME_LEFT:
                mi = next_market
            else:
                self._log(
                    f"Market gec acildi ({int(max(0, time_left))}s kaldi) — sonraki pencere bekleniyor"
                )
                return

        self._current_asset_ids = [mi["up_token_id"], mi["down_token_id"]]
        self._market_end_time = mi.get("end_time", 0)
        self._market_question = mi.get("question", "")
        self._market_budget = self.budget
        self.order_mgr.tick_size = mi.get("tick_size", 0.01)

        self.scanner.price_to_beat = 0.0
        self._start_btc_price = 0.0

        self._log(f"Market: {self._market_question}")
        self._log("Referans: ORDERBOOK ONLY")

        self.portfolio = PaperPortfolio()
        self.portfolio.budget = self.budget
        self.limit_engine.portfolio = self.portfolio
        self.order_mgr.active_orders.clear()
        self._spent = 0.0
        self._tick_count = 0
        self._risk_sell_triggered = False
        self._cost_rebalance_triggered = False
        self._cost_block_side = None
        self._reversal_target_side = None
        self._reversal_target_cost = 0.0
        self._orderbook_stop_hit = False
        self._orderbook_stop_side = None
        self._cheap_take_fired = set()

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
        self._market_budget = 0.0
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
        """SignalTracker hedge callback'i bu stratejide kullanilmiyor."""
        del req
        return

    async def _place_hedge(self, idx: int, side: str, price: float, size: float, budget: float):
        """Hedge emrini yerlestirir."""
        order = await self.order_mgr.place_limit_order(
            asset_id=self._current_asset_ids[idx],
            side=OrderSide.BUY, price=price, size=size, expiration_sec=90)
        if order:
            self.stats["orders_sent"] += 1
            self._log(f"HEDGE {side} | {size:.0f}sh @¢{price*100:.0f} = ${budget:.2f}")

    # ── Main Loop: guclu yonu takip et, reversal'da catch-up yap ──

    async def _main_loop(self):
        """Directional follow stratejisi.

        1. IZLE: orderbook ile guclu yonu topla (maks 30s)
        2. ALIM: aktif guclu yone alim yap
        3. Reversal: yeni guclu yone eski taraf kadar catch-up yatir
        4. Risk: worst-case zarar toplam butcenin %10'unu gecmesin
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
        self._active_direction = None
        self._direction_since = 0
        self._direction_changes = 0
        self._pending_direction = None
        self._pending_since = 0
        self._strategy_state = "IZLE"

        self._log(
            f"BASLA | ORDERBOOK ONLY | ${self._market_budget:.0f} | "
            "maks 30s IZLE → GUCLU YON TAKIP"
        )

        while self.bot_active:
            try:
                now = time.time()
                time_left = max(0, self._market_end_time - now) if self._market_end_time else 999
                elapsed = now - self._market_start_ts
                self._tick_count += 1
                self.order_mgr.expire_stale_orders()
                phase = min(NUM_PHASES - 1, int(elapsed / PHASE_DURATION))
                self._current_phase = phase

                if time_left <= 0:
                    await self._resolve_market()
                    return

                # ── Risk kontrolu: her tick ──
                self._check_max_loss()

                # ── Anlik yon sinyali: sadece orderbook ──
                current_dir = self._get_preferred_direction()

                if self._strategy_state != "BEKLE":
                    await self._buy_cheap_token_if_needed(time_left)

                # ══════════════════════════════════════
                # DURUM MAKINESI
                # ══════════════════════════════════════

                if self._strategy_state == "IZLE":
                    # ── 30s izleme: sinyal topla ──
                    self._izle_samples.append(
                        1.0 if current_dir == "UP" else -1.0 if current_dir == "DOWN" else 0.0
                    )

                    if current_dir and elapsed >= self._entry_wait_secs(current_dir):
                        self._lock_izle_signal()
                        self._activate_directional_entry(current_dir, early=True)
                    elif elapsed >= IZLE_DURATION:
                        self._lock_izle_signal()
                        if current_dir:
                            self._activate_directional_entry(current_dir)
                        else:
                            self._strategy_state = "ALIM"
                            self._log("YON BELIRSIZ — sinyal bekleniyor")

                elif self._strategy_state == "ALIM":
                    if self._orderbook_stop_hit:
                        self._strategy_state = "BEKLE"
                        self._log(
                            f"BEKLE | {self._orderbook_stop_side or '-'} orderbook ¢90 gordu — alim durduruldu"
                        )
                        await self.order_mgr.cancel_all()
                    elif time_left <= STOP_BEFORE_END:
                        self._strategy_state = "BEKLE"
                        self._log(f"BEKLE | son {STOP_BEFORE_END:.0f}s — alim durduruldu")
                        await self.order_mgr.cancel_all()
                    else:
                        # ══ AI AGENT KARAR MEKANIZMASI ══
                        if self.ai_agent.is_enabled:
                            await self._ai_tick(current_dir, time_left, elapsed)
                        else:
                            # Fallback: kural bazli strateji
                            if not self._active_direction and current_dir:
                                self._active_direction = current_dir
                                self._direction_since = now
                                self._log(f"YON AKTIFLESTI: {current_dir}")

                            # Yon degisimi kontrolu (3s onay)
                            if current_dir and current_dir != self._active_direction:
                                if self._pending_direction != current_dir:
                                    self._pending_direction = current_dir
                                    self._pending_since = now
                                elif now - self._pending_since >= self._direction_confirm_secs(current_dir):
                                    old_dir = self._active_direction
                                    self._active_direction = current_dir
                                    self._direction_since = now
                                    self._direction_changes += 1
                                    self._pending_direction = None
                                    self._arm_reversal_target(old_dir, current_dir)
                                    self._log(
                                        f"YON DEGISTI: {old_dir} → {current_dir} | "
                                        f"#{self._direction_changes} | reversal takip aktif"
                                    )
                            else:
                                self._pending_direction = None

                            if self._active_direction:
                                await self._buy_in_direction(self._active_direction, time_left)

                elif self._strategy_state == "BEKLE":
                    pass  # Son saniyeler, islem yok

                # ── Portfolio guncelle ──
                up_sh = self.portfolio.up.shares
                dn_sh = self.portfolio.down.shares
                if self.signal_tracker:
                    self.signal_tracker.update_portfolio(
                        up_sh, dn_sh,
                        self.portfolio.up.total_cost, self.portfolio.down.total_cost)

                # ── Maliyet dengeleme kontrolu (her 3s) ──
                if self._tick_count % 3 == 0 and self._strategy_state == "ALIM":
                    await self._rebalance_if_needed(phase, time_left)

                # ── Periyodik log (her 5s) ──
                if self._tick_count % 5 == 0:
                    tc = self.portfolio.total_cost()
                    up_cost = self.portfolio.up.total_cost
                    dn_cost = self.portfolio.down.total_cost
                    worst_loss = self._worst_case_loss_after(0, 0, 0, 0)
                    budget_cap = self._budget_cap()
                    risk_pct = worst_loss / budget_cap * 100 if budget_cap > 0 else 0
                    dir_label = self._active_direction or "—"
                    dir_secs = int(now - self._direction_since) if self._direction_since else 0
                    self._log(
                        f"[{self._strategy_state}] {dir_label}({dir_secs}s) | "
                        f"UP:{up_sh:.0f}sh DN:{dn_sh:.0f}sh | "
                        f"${tc:.0f}/{budget_cap:.0f} | "
                        f"risk:${worst_loss:.1f}(%{risk_pct:.0f}) | "
                        f"flip:{self._direction_changes} | {int(time_left)}s"
                    )

                self.stats["total_cycles"] += 1

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Loop: %s", e)

            await asyncio.sleep(1)

    # ── Yon Sinyali ───────────────────────────────────────────────

    def _signal_to_direction(self, signal_state: Optional[str] = None) -> Optional[str]:
        state = signal_state or self._signal_state
        if state == SignalState.STRONG_UP:
            return "UP"
        if state == SignalState.STRONG_DOWN:
            return "DOWN"
        return None

    def _get_preferred_direction(self) -> Optional[str]:
        """Yon tamamen orderbook sinyalinden gelir."""
        snap = self._latest_signal_snapshot()
        if not snap:
            return self._signal_to_direction()

        confirmed = self._signal_to_direction(snap.confirmed)
        if confirmed:
            return confirmed

        raw = self._signal_to_direction(snap.raw_signal)
        if not raw:
            return None

        edge = abs(getattr(snap, "edge_score", 0.0))
        persistence = getattr(snap, "persistence", 0.0)
        fast_track = (
            edge >= 0.08 and persistence >= 0.55 and
            (snap.confidence >= 0.70 or (snap.rapid_move and snap.confidence >= 0.60))
        )
        return raw if fast_track else None

    def _activate_directional_entry(self, direction: str, early: bool = False):
        self._active_direction = direction
        self._direction_since = time.time()
        self._strategy_state = "ALIM"
        prefix = "ERKEN GIRIS" if early else "REFERANS YON"
        self._log(f"{prefix}: {direction} | takipli alim basliyor")

    def _entry_wait_secs(self, direction: str) -> float:
        snap = self._latest_signal_snapshot()
        if not snap:
            return MIN_IZLE_BEFORE_ENTRY

        raw_dir = self._signal_to_direction(snap.raw_signal)
        confirmed_dir = self._signal_to_direction(snap.confirmed)
        edge = abs(getattr(snap, "edge_score", 0.0))
        persistence = getattr(snap, "persistence", 0.0)
        aligned = raw_dir == direction or confirmed_dir == direction
        if not aligned:
            return MIN_IZLE_BEFORE_ENTRY

        if snap.rapid_move and snap.confidence >= 0.85 and persistence >= 0.70 and edge >= 0.12:
            return 2.0
        if snap.confidence >= 0.72 and persistence >= 0.60 and edge >= 0.08:
            return 3.0
        return MIN_IZLE_BEFORE_ENTRY

    def _direction_confirm_secs(self, direction: str) -> float:
        snap = self._latest_signal_snapshot()
        if not snap:
            return DIRECTION_CONFIRM_SECS

        raw_dir = self._signal_to_direction(snap.raw_signal)
        if raw_dir != direction:
            return DIRECTION_CONFIRM_SECS

        edge = abs(getattr(snap, "edge_score", 0.0))
        persistence = getattr(snap, "persistence", 0.0)
        if snap.rapid_move and snap.confidence >= 0.85 and persistence >= 0.70 and edge >= 0.12:
            return 1.0
        if snap.confidence >= 0.70 and persistence >= 0.60 and edge >= 0.08:
            return 2.0
        return DIRECTION_CONFIRM_SECS

    # ── AI Agent Tick ─────────────────────────────────────────────

    async def _ai_tick(self, current_dir: Optional[str], time_left: float, elapsed: float):
        """AI agent'a market snapshot'i gonder, kararina gore islem yap."""
        up_book = self.market_feed.get_book(self._current_asset_ids[0]) if self.market_feed else None
        dn_book = self.market_feed.get_book(self._current_asset_ids[1]) if self.market_feed else None

        ctx = {
            "time_left": time_left,
            "elapsed": elapsed,
            "budget": self._budget_cap(),
            "remaining": self._remaining_budget(),
            "up_shares": self.portfolio.up.shares,
            "down_shares": self.portfolio.down.shares,
            "up_cost": self.portfolio.up.total_cost,
            "down_cost": self.portfolio.down.total_cost,
            "total_cost": self.portfolio.total_cost(),
            "worst_pnl": min(self.portfolio.up.shares, self.portfolio.down.shares) - self.portfolio.total_cost() if self.portfolio.total_cost() > 0 else 0,
            "risk_pct": self._worst_case_loss_after(0, 0, 0, 0) / self._budget_cap() * 100 if self._budget_cap() > 0 else 0,
            "up_bid": (up_book.best_bid or 0) if up_book else 0,
            "up_ask": (up_book.best_ask or 0) if up_book else 0,
            "dn_bid": (dn_book.best_bid or 0) if dn_book else 0,
            "dn_ask": (dn_book.best_ask or 0) if dn_book else 0,
            "up_bid_depth": sum(l.size for l in up_book.bids[:5]) if up_book and up_book.bids else 0,
            "dn_bid_depth": sum(l.size for l in dn_book.bids[:5]) if dn_book and dn_book.bids else 0,
            "active_orders": len(self.order_mgr.active_orders),
            "direction_changes": self._direction_changes,
            "current_direction": current_dir,
            "strategy_state": self._strategy_state,
            "recent_fills": self.stats["fills"],
        }

        decision = await self.ai_agent.decide(ctx)
        if not decision:
            # AI yanit vermedi — fallback: mevcut yone devam
            if self._active_direction:
                await self._buy_in_direction(self._active_direction, time_left)
            return

        self._ai_last_action = decision.action
        self._ai_reasoning = decision.reasoning

        # ── AI kararini execute et ──
        if decision.action == "HOLD":
            return

        elif decision.action == "CANCEL_ALL":
            await self.order_mgr.cancel_all()
            self._log(f"AI: CANCEL_ALL | {decision.reasoning}")

        elif decision.action in ("BUY_UP", "BUY_DOWN"):
            side = "UP" if decision.action == "BUY_UP" else "DOWN"
            idx = self._index_for_side(side)
            book = self.market_feed.get_book(self._current_asset_ids[idx]) if self.market_feed else None
            if not book:
                return

            budget = self._budget_cap() * (decision.budget_pct / 100.0)
            remaining = self._remaining_budget()
            budget = min(budget, remaining)

            if budget < MIN_TRADE_BUDGET:
                return

            # Yon guncelle
            if self._active_direction != side:
                old = self._active_direction
                self._active_direction = side
                self._direction_since = time.time()
                if old and old != side:
                    self._direction_changes += 1
                    self._arm_reversal_target(old, side)

            if decision.use_taker and book.best_ask and book.best_ask < ORDERBOOK_STOP_PRICE:
                budget = self._budget_fits_risk(side, book.best_ask, budget)
                if budget >= MIN_TRADE_BUDGET:
                    spent = await self._taker_buy(side, idx, book.best_ask, budget)
                    if spent > 0:
                        self._log(f"AI TAKER {side} | ${spent:.2f} | {decision.reasoning}")
            elif book.best_bid and book.best_bid < ORDERBOOK_STOP_PRICE:
                budget = self._budget_fits_risk(side, book.best_bid, budget)
                if budget >= MIN_TRADE_BUDGET:
                    placed = await self._maker_buy(side, idx, book.best_bid, budget)
                    if placed > 0:
                        self._log(f"AI MAKER {side} | ${placed:.2f} | {decision.reasoning}")

        elif decision.action in ("HEDGE_UP", "HEDGE_DOWN"):
            side = "UP" if decision.action == "HEDGE_UP" else "DOWN"
            idx = self._index_for_side(side)
            book = self.market_feed.get_book(self._current_asset_ids[idx]) if self.market_feed else None
            if not book or not book.best_ask:
                return

            budget = self._budget_cap() * (decision.budget_pct / 100.0)
            remaining = self._remaining_budget()
            budget = min(budget, remaining)

            if budget < MIN_TRADE_BUDGET:
                return

            # Hedge = taker (aninda dolum)
            budget = self._budget_fits_risk(side, book.best_ask, budget)
            if budget >= MIN_TRADE_BUDGET:
                spent = await self._taker_buy(side, idx, book.best_ask, budget)
                if spent > 0:
                    self._log(f"AI HEDGE {side} | ${spent:.2f} | {decision.reasoning}")

    def _ledger_for_side(self, side: str):
        return self.portfolio.up if side == "UP" else self.portfolio.down

    def _index_for_side(self, side: str) -> int:
        return 0 if side == "UP" else 1

    def _other_side(self, side: str) -> str:
        return "DOWN" if side == "UP" else "UP"

    def _arm_reversal_target(self, old_direction: Optional[str], new_direction: str):
        if not old_direction or old_direction == new_direction:
            self._reversal_target_side = None
            self._reversal_target_cost = 0.0
            return

        old_cost = self._ledger_for_side(old_direction).total_cost
        new_cost = self._ledger_for_side(new_direction).total_cost
        target_cost = max(old_cost, new_cost)
        if target_cost - new_cost < 1.0:
            self._reversal_target_side = None
            self._reversal_target_cost = 0.0
            return

        self._reversal_target_side = new_direction
        self._reversal_target_cost = target_cost
        self._log(
            f"REVERSAL HEDEF | {new_direction} catch-up ${max(0.0, target_cost - new_cost):.2f}"
        )

    def _planned_budget_for_direction(self, direction: str, remaining: float) -> float:
        if remaining < MIN_EFFECTIVE_BUDGET:
            return 0.0

        phase_room = self._phase_budget_room(direction, remaining)
        if phase_room < MIN_EFFECTIVE_BUDGET:
            return 0.0

        base_budget = max(MIN_TRADE_BUDGET, self._budget_cap() * BUDGET_PER_SEC)
        aggression = self._aggression_multiplier(direction)
        release_ratio = self._phase_release_ratio(direction)
        budget = max(base_budget * aggression, phase_room * release_ratio)
        budget = min(budget, phase_room, remaining)

        if self._reversal_target_side == direction:
            current_cost = self._ledger_for_side(direction).total_cost
            catch_up_budget = max(0.0, self._reversal_target_cost - current_cost)
            if catch_up_budget < 1.0:
                self._reversal_target_side = None
                self._reversal_target_cost = 0.0
                return budget
            budget = max(budget, min(catch_up_budget, phase_room))
            return min(max(MIN_TRADE_BUDGET, budget), remaining)

        return budget

    def _latest_signal_snapshot(self):
        if self.signal_tracker and self.signal_tracker.history:
            return self.signal_tracker.history[-1]
        return None

    def _signal_strength_for_direction(self, direction: str) -> tuple[float, bool]:
        snap = self._latest_signal_snapshot()
        if not snap:
            return 0.0, False

        confirmed_dir = self._signal_to_direction(snap.confirmed)
        raw_dir = self._signal_to_direction(snap.raw_signal)
        strength = 0.0
        aligns = False
        edge_strength = min(1.0, abs(getattr(snap, "edge_score", 0.0)) / 0.18)
        persistence = min(1.0, max(0.0, getattr(snap, "persistence", 0.0)))

        if confirmed_dir == direction:
            strength = max(
                strength,
                min(1.0, (snap.confidence * 0.55) + (edge_strength * 0.25) + (persistence * 0.20)),
            )
            aligns = True
        elif raw_dir == direction:
            strength = max(
                strength,
                min(1.0, (snap.confidence * 0.45) + (edge_strength * 0.35) + (persistence * 0.20)),
            )
            aligns = True

        rapid = bool(snap.rapid_move and aligns)
        return min(1.0, strength), rapid

    def _aggression_multiplier(self, direction: str) -> float:
        strength, rapid = self._signal_strength_for_direction(direction)
        phase_progress = (
            (self._current_phase + 1) / NUM_PHASES
            if 0 <= self._current_phase < NUM_PHASES else 0.0
        )
        sustain = 0.0
        if self._active_direction == direction and self._direction_since:
            sustain = min(1.0, max(0.0, time.time() - self._direction_since) / PHASE_DURATION)

        multiplier = 1.0 + (strength * 1.8) + (phase_progress * 0.4) + (sustain * 0.6)
        if rapid:
            multiplier += 0.4
        if self._reversal_target_side == direction:
            multiplier += 0.5

        return min(4.0, max(1.0, multiplier))

    def _phase_release_ratio(self, direction: str) -> float:
        strength, rapid = self._signal_strength_for_direction(direction)
        release = PHASE_RELEASE_BASE + (PHASE_RELEASE_MAX - PHASE_RELEASE_BASE) * strength
        if rapid:
            release += 0.10
        if self._reversal_target_side == direction:
            release += 0.10
        return min(0.90, max(PHASE_RELEASE_BASE, release))

    def _phase_budget_room(self, direction: str, remaining: float) -> float:
        if remaining < MIN_EFFECTIVE_BUDGET:
            return 0.0

        strength, rapid = self._signal_strength_for_direction(direction)
        phase_progress = (
            (self._current_phase + 1) / NUM_PHASES
            if 0 <= self._current_phase < NUM_PHASES else 0.0
        )
        phase_ratio = PHASE_BUDGET_BASE_PCT + (PHASE_BUDGET_MAX_PCT - PHASE_BUDGET_BASE_PCT) * strength
        phase_ratio += phase_progress * 0.02
        if rapid:
            phase_ratio += 0.02
        if self._reversal_target_side == direction:
            phase_ratio += 0.03

        phase_cap = min(remaining, self._budget_cap() * min(PHASE_BUDGET_MAX_PCT, phase_ratio))
        if 0 <= self._current_phase < NUM_PHASES:
            phase_cap = max(0.0, phase_cap - self._phase_spent[self._current_phase])
        return phase_cap

    def _record_phase_spend(self, amount: float):
        if amount <= 0:
            return
        if 0 <= self._current_phase < NUM_PHASES:
            self._phase_spent[self._current_phase] += amount

    async def _buy_cheap_token_if_needed(self, time_left: float):
        """¢15 alti token gorulurse ayni markette bir kez %5 butceyle taker al."""
        if time_left <= STOP_BEFORE_END or not self.market_feed:
            return

        remaining = self._remaining_budget()
        if remaining < MIN_EFFECTIVE_BUDGET:
            return

        desired_budget = min(self._budget_cap() * (CHEAP_TAKE_BUDGET_PCT / 100.0), remaining)
        if desired_budget < MIN_EFFECTIVE_BUDGET:
            return

        for side in ("UP", "DOWN"):
            if side in self._cheap_take_fired:
                continue

            idx = self._index_for_side(side)
            book = self.market_feed.get_book(self._current_asset_ids[idx]) if self.market_feed else None
            if not book or not book.best_ask:
                continue

            ask = book.best_ask
            if ask <= 0 or ask > CHEAP_TAKE_PRICE:
                continue

            budget = self._budget_fits_risk(side, ask, desired_budget)
            if budget < MIN_EFFECTIVE_BUDGET:
                continue

            spent = await self._taker_buy(side, idx, ask, budget)
            if spent > 0:
                self._cheap_take_fired.add(side)
                self._log(
                    f"ALT15 TAKER {side} | ${spent:.2f}@¢{ask*100:.0f} | "
                    f"butce%{CHEAP_TAKE_BUDGET_PCT:.0f}"
                )

    def _budget_fits_risk(self, side: str, price: float, desired_budget: float) -> float:
        if desired_budget < MIN_EFFECTIVE_BUDGET or price <= 0 or price >= 1.0:
            return 0.0

        max_loss = self._budget_cap() * (MAX_BUDGET_RISK_PCT / 100.0)
        candidate = desired_budget

        for _ in range(12):
            size = round(candidate / price, 1)
            if size < 5.0:
                return 0.0
            cost = size * price
            extra = (cost, size, 0.0, 0.0) if side == "UP" else (0.0, 0.0, cost, size)
            if self._worst_case_loss_after(*extra) <= max_loss + 1e-6:
                return cost
            candidate *= 0.5
            if candidate < MIN_EFFECTIVE_BUDGET:
                break

        return 0.0

    # ── Yone Gore Alim ────────────────────────────────────────────

    async def _buy_in_direction(self, direction: str, time_left: float):
        """Sadece aktif guclu yone alim yap, reversal'da catch-up hedefini uygula."""
        del time_left
        idx = self._index_for_side(direction)
        book = self.market_feed.get_book(self._current_asset_ids[idx]) if self.market_feed else None
        if not book or not book.best_bid:
            return

        bid = book.best_bid
        ask = book.best_ask or bid
        top_seen = max(book.best_bid or 0.0, book.best_ask or 0.0)
        if bid <= 0:
            return

        if top_seen >= ORDERBOOK_STOP_PRICE:
            self._orderbook_stop_hit = True
            self._orderbook_stop_side = direction
            return

        if self._cost_block_side == direction:
            return

        remaining = self._remaining_budget()
        if remaining < MIN_EFFECTIVE_BUDGET:
            return

        planned_budget = self._planned_budget_for_direction(direction, remaining)
        budget = self._budget_fits_risk(direction, ask, planned_budget)
        if budget < MIN_EFFECTIVE_BUDGET:
            return

        placed = await self._taker_buy(direction, idx, ask, budget)
        if placed > 0:
            tag = "REVERSAL" if self._reversal_target_side == direction else "TAKIP"
            self._log(
                f"{tag} ALIM {direction} | ${placed:.2f}@¢{ask*100:.0f} | "
                f"UP:${self.portfolio.up.total_cost:.2f} DN:${self.portfolio.down.total_cost:.2f}"
            )

    # ── Hedge: Yon Degistiginde Sifir Zarar Dengeleme ─────────────

    async def _hedge_to_zero_loss(self, new_direction: str, time_left: float):
        """Bu stratejide ayri hedge akisi yok; reversal catch-up kullaniliyor."""
        del new_direction, time_left
        return

    # ── Risk kontrolu ─────────────────────────────────────

    def _check_max_loss(self) -> bool:
        """Worst-case zarar butcenin %10'unu gecerse agir tarafi blokla."""
        up_payout = self.portfolio.up.shares + self.portfolio.up.total_revenue
        dn_payout = self.portfolio.down.shares + self.portfolio.down.total_revenue
        worst_loss = self._worst_case_loss_after(0, 0, 0, 0)
        max_loss = self._budget_cap() * (MAX_BUDGET_RISK_PCT / 100.0)

        if self.portfolio.total_cost() <= 0:
            self._cost_block_side = None
            self._cost_rebalance_triggered = False
            return False

        if worst_loss <= max_loss:
            self._cost_block_side = None
            self._cost_rebalance_triggered = False
            return False

        weak_side = "UP" if up_payout < dn_payout else "DOWN"
        heavy_side = self._other_side(weak_side)
        weak_idx = self._index_for_side(weak_side)

        if self._cost_block_side != heavy_side:
            self._cost_block_side = heavy_side
            self._log(
                f"RISK UYARI | zarar:${worst_loss:.2f}/${max_loss:.2f} | "
                f"{heavy_side} BLOKE — {weak_side} butceyi toparlamali"
            )

        if not self._cost_rebalance_triggered:
            self._cost_rebalance_triggered = True
            if self._active_direction == weak_side:
                catch_up_budget = max(
                    MIN_TRADE_BUDGET,
                    self._ledger_for_side(heavy_side).total_cost - self._ledger_for_side(weak_side).total_cost,
                )
                self._spawn_task(self._emergency_rebalance(weak_side, weak_idx, catch_up_budget))

        return True

    async def _emergency_rebalance(self, side: str, idx: int, budget: float):
        """Acil toparlama: zayif tarafi taker ile toparla."""
        book = self.market_feed.get_book(self._current_asset_ids[idx]) if self.market_feed else None
        if not book or not book.best_ask:
            self._log(f"ACIL TOPARLAMA {side} BASARISIZ | ask yok")
            return

        ask = book.best_ask
        if ask <= 0 or ask >= ORDERBOOK_STOP_PRICE:
            return

        remaining = self._remaining_budget()
        catch_up_budget = min(budget, remaining)
        catch_up_budget = self._budget_fits_risk(side, ask, catch_up_budget)
        if catch_up_budget < MIN_EFFECTIVE_BUDGET:
            return

        cost = await self._taker_buy(side, idx, ask, catch_up_budget)
        if cost > 0:
            self._log(
                f"ACIL TOPARLAMA {side} | +${cost:.2f} @¢{ask*100:.0f} | "
                f"UP:${self.portfolio.up.total_cost:.2f} DN:${self.portfolio.down.total_cost:.2f}"
            )

    async def _risk_reduce(self, side: str, idx: int, shares: float):
        """Risk azaltma: fazla tarafin bir kismini sat."""
        book = self.market_feed.get_book(self._current_asset_ids[idx]) if self.market_feed else None
        if not book or not book.best_ask:
            self._log(f"RISK SATIS BASARISIZ {side} | ask yok")
            return

        ask = book.best_ask
        if ask <= 0 or ask >= 1.0:
            return

        order = await self.order_mgr.place_limit_order(
            asset_id=self._current_asset_ids[idx],
            side=OrderSide.SELL, price=ask, size=shares,
            expiration_sec=90, taker=True)

        if order:
            # Taker satis: aninda doldugu varsayilir
            ledger = self.portfolio.up if side == "UP" else self.portfolio.down
            ledger.record_sell(ask, shares, 0)
            revenue = ask * shares
            self._log(
                f"RISK SATIS {side} | {shares:.0f}sh @¢{ask*100:.0f} = ${revenue:.2f} | "
                f"UP:{self.portfolio.up.shares:.0f} DN:{self.portfolio.down.shares:.0f}"
            )
            self.stats["fills"] += 1

    # ── Risk Hesaplari ──────────────────────────────────────────

    def _worst_case_after(self, extra_up_cost: float, extra_up_sh: float,
                          extra_dn_cost: float, extra_dn_sh: float) -> float:
        """Geri uyumluluk: worst-case zarari butceye gore yuzde olarak dondur."""
        loss = self._worst_case_loss_after(extra_up_cost, extra_up_sh, extra_dn_cost, extra_dn_sh)
        if self._budget_cap() <= 0:
            return 0.0
        return loss / self._budget_cap() * 100

    def _worst_case_loss_after(self, extra_up_cost: float, extra_up_sh: float,
                               extra_dn_cost: float, extra_dn_sh: float) -> float:
        """Simdi + planlanan alim sonrasi mutlak worst-case zarari hesapla."""
        cost = self.portfolio.total_cost() + extra_up_cost + extra_dn_cost
        if cost <= 0:
            return 0.0
        up_sh = self.portfolio.up.shares + extra_up_sh
        dn_sh = self.portfolio.down.shares + extra_dn_sh
        up_rev = self.portfolio.up.total_revenue
        dn_rev = self.portfolio.down.total_revenue
        worst_pnl = min((up_sh + up_rev) - cost, (dn_sh + dn_rev) - cost)
        if worst_pnl >= 0:
            return 0.0
        return abs(worst_pnl)

    async def _rebalance_if_needed(self, phase: int, time_left: float):
        """Reversal hedefi aciksa eksik tarafi hizlica yetistir."""
        del phase
        if self._strategy_state != "ALIM" or not self._active_direction:
            return

        if self._reversal_target_side != self._active_direction:
            return

        weak_side = self._active_direction
        weak_idx = self._index_for_side(weak_side)
        catch_up_budget = max(0.0, self._reversal_target_cost - self._ledger_for_side(weak_side).total_cost)
        if catch_up_budget < MIN_TRADE_BUDGET:
            self._reversal_target_side = None
            self._reversal_target_cost = 0.0
            return

        book = self.market_feed.get_book(self._current_asset_ids[weak_idx]) if self.market_feed else None
        if not book or not book.best_bid:
            return
        bid = book.best_bid
        if bid <= 0 or bid >= ORDERBOOK_STOP_PRICE:
            return

        remaining = self._remaining_budget()
        rebalance_cost = min(catch_up_budget, remaining)
        rebalance_cost = self._budget_fits_risk(weak_side, bid, rebalance_cost)
        if rebalance_cost < MIN_EFFECTIVE_BUDGET:
            return

        placed = await self._maker_buy(weak_side, weak_idx, bid, rebalance_cost)
        if placed > 0:
            self._log(
                f"REBALANCE {weak_side} | hedef:${self._reversal_target_cost:.2f} | "
                f"UP:${self.portfolio.up.total_cost:.2f} DN:${self.portfolio.down.total_cost:.2f} | "
                f"+${placed:.2f}@¢{bid*100:.0f} | {int(time_left)}s"
            )

    async def _maker_buy(self, side: str, idx: int, bid_price: float, budget: float) -> float:
        """Orderbook bid seviyelerine dagitarak maker BUY emirleri koy.

        ¢90+ fiyattan emir GONDERMEZ.
        Maliyet bloklu taraf icin emir gondermez.
        """
        # Maliyet bloklama: agir tarafa emir gonderme
        if self._cost_block_side == side:
            return 0.0

        budget = min(budget, self._remaining_budget())
        if bid_price >= ORDERBOOK_STOP_PRICE:
            return 0.0
        if budget < MIN_EFFECTIVE_BUDGET or bid_price <= 0:
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
            if price <= 0 or price >= ORDERBOOK_STOP_PRICE:
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

        self._record_phase_spend(total_placed)
        return total_placed

    async def _taker_buy(self, side: str, idx: int, ask_price: float, budget: float) -> float:
        """Best ask'tan taker BUY emri gonder. Harcanan tutari dondurur."""
        budget = min(budget, self._remaining_budget())
        if budget < MIN_EFFECTIVE_BUDGET or ask_price <= 0 or ask_price >= 1.0:
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
            ledger = self.portfolio.up if side == "UP" else self.portfolio.down
            ledger.record_buy(ask_price, size, 0)
            self._archive_fill(side, ask_price, size, cost)
            self._record_phase_spend(cost)
            self.stats["orders_sent"] += 1
            self.stats["fills"] += 1
            return cost
        return 0.0

    # ── Sinyal: start noktasindan sapma ───────────────────────────

    def _lock_izle_signal(self):
        """IZLE fazinda toplanan orderbook yon skorunu kilitle."""
        if not self._izle_samples:
            self._izle_signal = SignalState.NEUTRAL
            self._izle_avg = 0.0
            self._log("IZLE SONUC | veri yok → NEUTRAL")
            return

        avg = sum(self._izle_samples) / len(self._izle_samples)
        self._izle_avg = avg

        # Ortalama orderbook yon skoru yorumlama
        if avg > 0.2:
            self._izle_signal = SignalState.STRONG_UP
        elif avg < -0.2:
            self._izle_signal = SignalState.STRONG_DOWN
        else:
            self._izle_signal = SignalState.NEUTRAL

        direction = "UP" if self._izle_signal == SignalState.STRONG_UP else \
                    "DN" if self._izle_signal == SignalState.STRONG_DOWN else "—"
        self._log(
            f"IZLE SONUC | {len(self._izle_samples)} ornek | "
            f"ort OB skoru: {avg:+.4f} | YON: {direction}"
        )

    # ── Market Resolve ───────────────────────────────────────────

    def _resolve_winner_from_orderbook(self) -> str:
        """Kazanan tarafi sadece orderbook/trade verisinden tahmin et."""
        up_p, dn_p = self._get_prices()
        if up_p > 0 or dn_p > 0:
            if up_p == dn_p:
                if self._active_direction:
                    return self._active_direction
                return "UP"
            return "UP" if up_p > dn_p else "DOWN"

        if self.market_feed and self._current_asset_ids:
            up_trade = self.market_feed.last_trades.get(self._current_asset_ids[0])
            dn_trade = self.market_feed.last_trades.get(self._current_asset_ids[1])
            up_last = up_trade.price if up_trade else 0.0
            dn_last = dn_trade.price if dn_trade else 0.0
            if up_last > 0 or dn_last > 0:
                return "UP" if up_last >= dn_last else "DOWN"

        if self._active_direction:
            return self._active_direction
        sig_dir = self._signal_to_direction()
        if sig_dir:
            return sig_dir
        return "UP" if self.portfolio.up.shares >= self.portfolio.down.shares else "DOWN"

    async def _resolve_market(self):
        self._log("Market kapandi...")

        up_sh = self.portfolio.up.shares
        dn_sh = self.portfolio.down.shares
        paired = min(up_sh, dn_sh)

        winner = self._resolve_winner_from_orderbook()

        traded = self.portfolio.total_shares() > 0
        if traded:
            res = self.portfolio.resolve_market(winner)
            pnl = res["resolved_pnl"]
            pnl_pct = (pnl / res["total_cost"] * 100) if res["total_cost"] > 0 else 0
            w_tr = "YUKARI" if winner == "UP" else "ASAGI"

            self._log(f"━━ {w_tr} KAZANDI ━━")
            self._log(
                f"Cift:{paired:.0f} | Odeme:${res['payout']:.2f} | "
                f"Maliyet:${res['total_cost']:.2f} | K/Z:${pnl:+.2f} (%{pnl_pct:+.1f})"
            )

        else:
            self._log("Islem yok — uygun sinyal/risk penceresi bulunamadi")

        snapshot = self.portfolio.snapshot_for_history(
            question=self._market_question, winner=winner,
            btc_close=0, ptb=0,
        )
        snapshot["ticks"] = self._tick_count
        snapshot["paired"] = paired
        snapshot["traded"] = traded
        snapshot["budget_open"] = round(self._market_budget, 2)
        snapshot["budget"] = round(self._budget_cap(), 2)
        self.market_history.append(snapshot)
        self._save_history()

        cumulative = sum(h.get("resolved_pnl", 0) for h in self.market_history)
        total = len(self.market_history)
        wins = sum(1 for h in self.market_history if h.get("resolved_pnl", 0) > 0)
        self._log(
            f"HISTORY KAYIT | #{total} | {'TRADE' if traded else 'NO TRADE'} | "
            f"Bu:${snapshot.get('resolved_pnl', 0):+.2f} | Toplam:${cumulative:+.2f} | Kazanma:{wins}/{total}"
        )

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

            ledger.record_buy(order.price, fill_qty, 0)
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
