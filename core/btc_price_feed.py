"""Real-time BTC/USD price feed via Binance WebSocket.

Tracks current price, 5-min window opening price, and computes
a directional signal (-1 to +1) for quote skewing.
"""

import asyncio
import json
import ssl
import time
from typing import Optional

import certifi
import websockets

from utils.logger import setup_logger

log = setup_logger("btc_feed")

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
WINDOW_SEC = 300  # 5-minute windows aligned to UTC


class BtcPriceFeed:
    """Connects to Binance and provides directional signal for quote engine."""

    def __init__(self):
        self.current_price: float = 0.0
        self.window_open_price: float = 0.0   # Binance-based fallback
        self.price_to_beat: float = 0.0       # Polymarket/Chainlink official
        self.window_start_ts: float = 0.0
        self._running = False
        self._trade_count = 0

    @property
    def reference_price(self) -> float:
        """The official reference price: Polymarket's priceToBeat if available,
        otherwise Binance window open as fallback."""
        if self.price_to_beat > 0:
            return self.price_to_beat
        return self.window_open_price

    @property
    def change_pct(self) -> float:
        """Price change vs reference (Chainlink price to beat), as percentage."""
        ref = self.reference_price
        if ref <= 0:
            return 0.0
        return (self.current_price - ref) / ref * 100

    @property
    def direction(self) -> float:
        """Directional signal from -1.0 (strong bearish) to +1.0 (strong bullish).

        Maps change_pct through a sigmoid-like curve:
          ±0.05% → ±0.25 (weak signal)
          ±0.10% → ±0.50 (medium signal)
          ±0.20% → ±0.75 (strong signal)
          ±0.50% → ±0.95 (very strong)
        """
        pct = self.change_pct
        if pct == 0:
            return 0.0
        # Tanh-based sigmoid: tanh(pct / 0.15) gives nice curve
        import math
        return max(-1.0, min(1.0, math.tanh(pct / 0.15)))

    @property
    def signal_label(self) -> str:
        d = self.direction
        if d > 0.6:
            return "STRONG UP"
        elif d > 0.2:
            return "UP"
        elif d > -0.2:
            return "NEUTRAL"
        elif d > -0.6:
            return "DOWN"
        else:
            return "STRONG DOWN"

    def state(self) -> dict:
        """State dict for dashboard."""
        return {
            "current": self.current_price,
            "price_to_beat": self.price_to_beat,
            "window_open": self.window_open_price,
            "reference": self.reference_price,
            "change_pct": round(self.change_pct, 4),
            "direction": round(self.direction, 4),
            "signal": self.signal_label,
            "trades": self._trade_count,
        }

    async def connect(self):
        """Connect to Binance WebSocket and stream BTC trades."""
        self._running = True
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        while self._running:
            try:
                async with websockets.connect(BINANCE_WS, ssl=ssl_ctx) as ws:
                    log.info("Binance BTC feed connected")
                    self._reset_window()

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            price = float(msg.get("p", 0))
                            if price > 0:
                                self.current_price = price
                                self._trade_count += 1

                                # Set window open on first tick
                                if self.window_open_price <= 0:
                                    self.window_open_price = price

                                # Check window rotation
                                self._check_window()

                                # Log periodically
                                if self._trade_count % 500 == 0:
                                    log.info(
                                        "BTC $%.2f | open $%.2f | %+.3f%% | %s",
                                        self.current_price,
                                        self.window_open_price,
                                        self.change_pct,
                                        self.signal_label,
                                    )
                        except (json.JSONDecodeError, ValueError):
                            continue

            except websockets.ConnectionClosed:
                log.warning("Binance WS disconnected, reconnecting in 2s...")
                await asyncio.sleep(2)
            except Exception as e:
                log.error("Binance WS error: %s, reconnecting in 5s...", e)
                await asyncio.sleep(5)

    def _reset_window(self):
        """Start a new 5-minute window."""
        now = time.time()
        self.window_start_ts = now - (now % WINDOW_SEC)
        if self.current_price > 0:
            self.window_open_price = self.current_price
            log.info(
                "Window reset: open=$%.2f at %s",
                self.window_open_price,
                time.strftime("%H:%M:%S", time.gmtime(self.window_start_ts)),
            )

    def _check_window(self):
        """Check if we've crossed into a new 5-min window."""
        now = time.time()
        current_window = now - (now % WINDOW_SEC)
        if current_window > self.window_start_ts:
            self._reset_window()

    async def disconnect(self):
        self._running = False
        log.info("BTC feed disconnected")
