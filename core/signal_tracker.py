"""Signal Tracker — 200ms (5x/s) fiyat kontrolu, fazli sinyal sistemi.

300 saniyelik market penceresi kullanilir.
Her faz sonunda sinyal yeniden degerlendirilir.
Faz ici 200ms'de bir kontrol yapilir.

Risk hedge:
  Guclu sinyal ile tek tarafa agirliklı girildiginde,
  zayif tarafin fiyati ¢20 altina duserse ucuz hedge alimi yapilir.
  Hedef: max $5 risk (kayip siniri).

Hizli risk sifirlama:
  Fiyat ani degistiginde (¢5+ hareket / 5 kontrol) zayif tarafa
  aninda hedge emri girilir.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

from utils.logger import setup_logger

log = setup_logger("signal")

SIGNAL_THRESHOLD_PCT = 10.0  # %10 makas → sinyal tetikle
CHECK_INTERVAL = 0.2         # 200ms = saniyede 5 kontrol
MARKET_WINDOW_SEC = 300.0
DEFAULT_PHASE_COUNT = 10
CONFIRMATION_COUNT = 3       # 3 ardisik onay
COOLDOWN_SEC = 2.0
DEPTH_LEVELS = 5
EDGE_SCORE_THRESHOLD = 0.045
EDGE_SCORE_STRONG = 0.12
TREND_WINDOW = 8             # ~1.6s trend penceresi
RAW_HISTORY_WINDOW = 12      # ~2.4s persistence/noise penceresi

# Risk hedge
HEDGE_PRICE_THRESHOLD = 0.20  # ¢20 altina dusunce ucuz hedge al
MAX_RISK_USD = 5.0            # Maksimum kayip siniri
HEDGE_CHECK_INTERVAL = 1.0    # Hedge kontrolu 1 saniye

# Hizli risk sifirlama
RAPID_MOVE_CENTS = 5.0        # ¢5 hareket = hizli degisim
RAPID_WINDOW = 5              # Son 5 kontrol icinde


class SignalState:
    NEUTRAL = "NEUTRAL"
    STRONG_UP = "STRONG_UP"
    STRONG_DOWN = "STRONG_DOWN"


@dataclass
class SignalSnapshot:
    ts: float
    up_mid: float
    dn_mid: float
    spread_pct: float
    raw_signal: str
    confirmed: str
    confidence: float
    phase: int
    rapid_move: bool = False
    up_ref_move: float = 0.0
    dn_ref_move: float = 0.0
    ref_edge: float = 0.0
    pressure_bias: float = 0.0
    trend_bias: float = 0.0
    edge_score: float = 0.0
    persistence: float = 0.0
    flip_risk: float = 0.0


@dataclass
class HedgeRequest:
    """Hedge alim talebi — strategy'ye iletilir."""
    side: str        # "UP" veya "DOWN" (ucuz taraf)
    price: float     # Hedef fiyat
    max_cost: float  # Maksimum harcama


class SignalTracker:
    """Kullanici tanimli fazli sinyal sistemi + risk hedge + hizli risk sifirlama."""

    def __init__(
        self,
        book_getter: Callable,
        phase_count: int = DEFAULT_PHASE_COUNT,
        market_window_sec: float = MARKET_WINDOW_SEC,
    ):
        self._book_getter = book_getter
        self._up_asset_id: str = ""
        self._dn_asset_id: str = ""
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.market_window_sec = market_window_sec
        self.phase_count = max(1, int(phase_count))
        self.phase_duration = self.market_window_sec / self.phase_count

        # State
        self.state = SignalState.NEUTRAL
        self._prev_state = SignalState.NEUTRAL
        self._confirmation_count = 0
        self._last_change_ts: float = 0

        # Faz takibi
        self._market_start_ts: float = 0
        self.current_phase: int = 0
        self._phase_signals: list[str] = []  # Her fazin sinyal sonucu

        # Fiyat gecmisi (hizli hareket tespiti icin)
        self._price_history: list[tuple[float, float]] = []  # [(up_mid, dn_mid), ...]
        self._raw_history: list[str] = []
        self._ref_up_mid: float = 0.0
        self._ref_dn_mid: float = 0.0

        # Callbacks
        self._on_signal: Optional[Callable] = None
        self._on_hedge: Optional[Callable] = None  # Hedge talebi

        # Portfolio bilgisi (strategy tarafindan set edilir)
        self.up_shares: float = 0
        self.dn_shares: float = 0
        self.up_cost: float = 0
        self.dn_cost: float = 0

        # History
        self.history: list[SignalSnapshot] = []
        self.checks = 0
        self.signals_fired = 0
        self.hedges_requested = 0

    def set_phase_count(self, phase_count: int):
        self.phase_count = max(1, int(phase_count))
        self.phase_duration = self.market_window_sec / self.phase_count
        self.current_phase = min(self.current_phase, self.phase_count - 1)

    def configure(self, up_asset_id: str, dn_asset_id: str):
        self._up_asset_id = up_asset_id
        self._dn_asset_id = dn_asset_id
        self._market_start_ts = time.time()
        self.current_phase = 0
        self._phase_signals = []
        self._price_history = []
        self._raw_history = []
        self._ref_up_mid = 0.0
        self._ref_dn_mid = 0.0
        self.state = SignalState.NEUTRAL
        self._prev_state = SignalState.NEUTRAL
        self._confirmation_count = 0
        self._last_change_ts = 0.0

    def _book_pressure(self, book) -> float:
        bid_depth = sum(level.size for level in book.bids[:DEPTH_LEVELS]) if book and book.bids else 0.0
        ask_depth = sum(level.size for level in book.asks[:DEPTH_LEVELS]) if book and book.asks else 0.0
        total = bid_depth + ask_depth
        if total <= 0:
            return 0.0
        return (bid_depth - ask_depth) / total

    def _direction_persistence(self, candidate: str) -> float:
        window = (self._raw_history + [candidate])[-RAW_HISTORY_WINDOW:]
        directional = [state for state in window if state != SignalState.NEUTRAL]
        if not directional or candidate == SignalState.NEUTRAL:
            return 0.0
        return sum(1 for state in directional if state == candidate) / len(directional)

    def _flip_risk(self, candidate: str) -> float:
        window = (self._raw_history + [candidate])[-RAW_HISTORY_WINDOW:]
        directional = [state for state in window if state != SignalState.NEUTRAL]
        if len(directional) < 2:
            return 0.0
        flips = sum(1 for i in range(1, len(directional)) if directional[i] != directional[i - 1])
        return flips / (len(directional) - 1)

    def _required_confirmations(self, raw: str, confidence: float, rapid_move: bool, persistence: float) -> int:
        if raw == SignalState.NEUTRAL:
            return max(1, CONFIRMATION_COUNT - 1)
        if rapid_move and confidence >= 0.85 and persistence >= 0.70:
            return 1
        if confidence >= 0.65 and persistence >= 0.55:
            return 2
        return CONFIRMATION_COUNT

    def _cooldown_sec(self, raw: str, confidence: float, rapid_move: bool, persistence: float) -> float:
        if raw == SignalState.NEUTRAL:
            return max(0.5, COOLDOWN_SEC * 0.5)
        if rapid_move and confidence >= 0.85 and persistence >= 0.70:
            return 0.75
        if confidence >= 0.65:
            return 1.0
        return COOLDOWN_SEC

    def on_signal(self, callback: Callable):
        """fn(state, snapshot)"""
        self._on_signal = callback

    def on_hedge(self, callback: Callable):
        """fn(hedge_request: HedgeRequest)"""
        self._on_hedge = callback

    def update_portfolio(self, up_sh: float, dn_sh: float, up_cost: float, dn_cost: float):
        """Strategy her tick'te portfolio bilgisini gunceller."""
        self.up_shares = up_sh
        self.dn_shares = dn_sh
        self.up_cost = up_cost
        self.dn_cost = dn_cost

    async def start(self):
        self._running = True
        self._market_start_ts = time.time()
        self._task = asyncio.create_task(self._loop())
        log.info(
            "SignalTracker baslatildi (200ms, %d faz x %.1fs)",
            self.phase_count,
            self.phase_duration,
        )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        while self._running:
            try:
                self._check()
            except Exception as e:
                log.error("Signal check: %s", e)
            await asyncio.sleep(CHECK_INTERVAL)

    def _check(self):
        if not self._up_asset_id or not self._dn_asset_id:
            return

        up_book = self._book_getter(self._up_asset_id)
        dn_book = self._book_getter(self._dn_asset_id)
        if not up_book or not dn_book:
            return

        up_mid = up_book.mid_price
        dn_mid = dn_book.mid_price
        if not up_mid or not dn_mid or up_mid <= 0 or dn_mid <= 0:
            return

        if self._ref_up_mid <= 0 or self._ref_dn_mid <= 0:
            self._ref_up_mid = up_mid
            self._ref_dn_mid = dn_mid

        self.checks += 1
        now = time.time()

        # ── Faz hesabi ──
        elapsed = now - self._market_start_ts if self._market_start_ts > 0 else 0
        new_phase = min(self.phase_count - 1, int(elapsed / self.phase_duration))
        phase_changed = new_phase != self.current_phase
        if phase_changed:
            self._phase_signals.append(self.state)
            self.current_phase = new_phase
            log.info("FAZ %d/%d | sinyal: %s | sure: %.0fs", new_phase + 1, self.phase_count, self.state, elapsed)

        # ── Makas hesabi ──
        min_price = min(up_mid, dn_mid)
        spread_pct = abs(up_mid - dn_mid) / min_price * 100 if min_price > 0 else 0
        up_ref_move = up_mid - self._ref_up_mid
        dn_ref_move = dn_mid - self._ref_dn_mid
        ref_edge = up_ref_move - dn_ref_move
        pressure_bias = (self._book_pressure(up_book) - self._book_pressure(dn_book)) / 2.0

        # ── Hizli hareket tespiti ──
        self._price_history.append((up_mid, dn_mid))
        if len(self._price_history) > 50:
            self._price_history = self._price_history[-50:]

        rapid_move = False
        trend_bias = 0.0
        if len(self._price_history) >= RAPID_WINDOW:
            old_up, old_dn = self._price_history[-RAPID_WINDOW]
            up_move = abs(up_mid - old_up) * 100  # cent cinsinden
            dn_move = abs(dn_mid - old_dn) * 100
            if up_move >= RAPID_MOVE_CENTS or dn_move >= RAPID_MOVE_CENTS:
                rapid_move = True
        if len(self._price_history) >= TREND_WINDOW:
            old_up, old_dn = self._price_history[-TREND_WINDOW]
            trend_bias = (up_mid - old_up) - (dn_mid - old_dn)

        mid_edge = up_mid - dn_mid
        edge_score = (
            (mid_edge * 0.45)
            + (ref_edge * 0.30)
            + (trend_bias * 0.15)
            + (pressure_bias * 0.10)
        )
        if abs(ref_edge) >= EDGE_SCORE_STRONG or abs(trend_bias) >= 0.05:
            rapid_move = True

        provisional_raw = SignalState.NEUTRAL
        if edge_score >= EDGE_SCORE_THRESHOLD:
            provisional_raw = SignalState.STRONG_UP
        elif edge_score <= -EDGE_SCORE_THRESHOLD:
            provisional_raw = SignalState.STRONG_DOWN

        persistence = self._direction_persistence(provisional_raw)
        flip_risk = self._flip_risk(provisional_raw)
        confidence = min(1.0, abs(edge_score) / EDGE_SCORE_STRONG) if provisional_raw != SignalState.NEUTRAL else 0.0
        confidence = max(0.0, min(1.0, confidence * (1.0 - (flip_risk * 0.35)) + (persistence * 0.15)))

        # ── Ham sinyal ──
        raw = provisional_raw
        if spread_pct >= SIGNAL_THRESHOLD_PCT:
            raw = SignalState.STRONG_UP if up_mid > dn_mid else SignalState.STRONG_DOWN
            confidence = max(confidence, min(1.0, spread_pct / 35.0))
        if raw != SignalState.NEUTRAL and confidence < 0.33 and not rapid_move:
            raw = SignalState.NEUTRAL
        if raw != SignalState.NEUTRAL and persistence < 0.45 and confidence < 0.75 and not rapid_move:
            raw = SignalState.NEUTRAL
        if raw == SignalState.NEUTRAL:
            confidence = 0.0
            persistence = 0.0

        self._raw_history.append(raw)
        if len(self._raw_history) > 50:
            self._raw_history = self._raw_history[-50:]

        # ── Onay ──
        if raw == self._prev_state:
            self._confirmation_count += 1
        else:
            self._confirmation_count = 1
            self._prev_state = raw

        required = self._required_confirmations(raw, confidence, rapid_move, persistence)
        cooldown = self._cooldown_sec(raw, confidence, rapid_move, persistence)
        if self._confirmation_count >= required:
            if now - self._last_change_ts >= cooldown:
                if raw != self.state:
                    old = self.state
                    self.state = raw
                    self._last_change_ts = now
                    self.signals_fired += 1
                    log.info(
                        "SINYAL: %s → %s | UP¢%.1f DN¢%.1f makas%%%.1f faz:%d",
                        old, raw, up_mid * 100, dn_mid * 100, spread_pct, self.current_phase + 1
                    )
                    if self._on_signal:
                        snap = SignalSnapshot(
                            ts=now, up_mid=up_mid, dn_mid=dn_mid,
                            spread_pct=spread_pct, raw_signal=raw,
                            confirmed=raw, confidence=confidence,
                            phase=self.current_phase, rapid_move=rapid_move,
                            up_ref_move=up_ref_move, dn_ref_move=dn_ref_move,
                            ref_edge=ref_edge, pressure_bias=pressure_bias,
                            trend_bias=trend_bias, edge_score=edge_score,
                            persistence=persistence, flip_risk=flip_risk,
                        )
                        self._on_signal(raw, snap)

        # ── Risk hedge kontrolu ──
        self._check_hedge(up_mid, dn_mid, rapid_move)

        # ── Snapshot ──
        snap = SignalSnapshot(
            ts=now, up_mid=up_mid, dn_mid=dn_mid,
            spread_pct=spread_pct, raw_signal=raw,
            confirmed=self.state, confidence=confidence,
            phase=self.current_phase, rapid_move=rapid_move,
            up_ref_move=up_ref_move, dn_ref_move=dn_ref_move,
            ref_edge=ref_edge, pressure_bias=pressure_bias,
            trend_bias=trend_bias, edge_score=edge_score,
            persistence=persistence, flip_risk=flip_risk,
        )
        self.history.append(snap)
        if len(self.history) > 50:
            self.history = self.history[-50:]

    def _check_hedge(self, up_mid: float, dn_mid: float, rapid_move: bool):
        """Risk hedge: tek tarafa agirlik varsa ucuz hedge al."""
        if not self._on_hedge:
            return

        # Dengesizlik var mi?
        up_sh = self.up_shares
        dn_sh = self.dn_shares
        if up_sh < 5 and dn_sh < 5:
            return  # Henuz pozisyon yok

        max_sh = max(up_sh, dn_sh)
        min_sh = min(up_sh, dn_sh)
        if max_sh <= 0:
            return

        gap_ratio = (max_sh - min_sh) / max_sh  # 0-1 arasi dengesizlik

        # Zayif taraf ve fiyati
        if up_sh > dn_sh:
            weak_side = "DOWN"
            weak_price = dn_mid
            strong_cost = self.up_cost
        else:
            weak_side = "UP"
            weak_price = up_mid
            strong_cost = self.dn_cost

        # ── Kosul 1: Ucuz hedge (¢20 altinda) ──
        if weak_price <= HEDGE_PRICE_THRESHOLD and gap_ratio > 0.3:
            # Kac adet hedge almaliyiz?
            # Hedef: max $5 risk → max_sh * weak_price <= $5 degerinde hedge
            needed_shares = max(0, max_sh - min_sh)
            # Maliyet siniri: MAX_RISK_USD
            max_shares = MAX_RISK_USD / max(weak_price, 0.01)
            hedge_shares = min(needed_shares, max_shares)
            hedge_cost = hedge_shares * weak_price

            if hedge_cost >= 1.0 and hedge_shares >= 5:
                self.hedges_requested += 1
                self._on_hedge(HedgeRequest(
                    side=weak_side,
                    price=weak_price,
                    max_cost=min(hedge_cost, MAX_RISK_USD),
                ))
                log.info(
                    "HEDGE %s | ¢%.0f x %.0f sh = $%.2f | gap%%%.0f",
                    weak_side, weak_price * 100, hedge_shares, hedge_cost, gap_ratio * 100
                )

        # ── Kosul 2: Hizli fiyat degisimi → acil hedge ──
        elif rapid_move and gap_ratio > 0.5:
            # Fiyat hizla degisiyor ve cok dengesiziz → acil hedge
            emergency_cost = min(MAX_RISK_USD, strong_cost * 0.05)  # Maliyetin %5'i
            if emergency_cost >= 1.0:
                self.hedges_requested += 1
                self._on_hedge(HedgeRequest(
                    side=weak_side,
                    price=weak_price,
                    max_cost=emergency_cost,
                ))
                log.info(
                    "ACIL HEDGE %s | ¢%.0f $%.2f | hizli hareket + gap%%%.0f",
                    weak_side, weak_price * 100, emergency_cost, gap_ratio * 100
                )

    def current(self) -> dict:
        last = self.history[-1] if self.history else None
        return {
            "state": self.state,
            "up_mid": last.up_mid if last else 0,
            "dn_mid": last.dn_mid if last else 0,
            "spread_pct": round(last.spread_pct, 1) if last else 0,
            "confidence": round(last.confidence, 2) if last else 0,
            "phase": self.current_phase + 1,
            "total_phases": self.phase_count,
            "rapid_move": last.rapid_move if last else False,
            "ref_edge": round(last.ref_edge, 4) if last else 0,
            "pressure_bias": round(last.pressure_bias, 4) if last else 0,
            "trend_bias": round(last.trend_bias, 4) if last else 0,
            "edge_score": round(last.edge_score, 4) if last else 0,
            "persistence": round(last.persistence, 2) if last else 0,
            "flip_risk": round(last.flip_risk, 2) if last else 0,
            "checks": self.checks,
            "signals_fired": self.signals_fired,
            "hedges": self.hedges_requested,
        }
