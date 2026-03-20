"""WebSocket-based market data handler for Polymarket CLOB."""

import asyncio
import contextlib
import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import certifi
import websockets

from utils.logger import setup_logger

log = setup_logger("market_data")

RX_STALE_SEC = 15.0
BOOK_STALE_SEC = 45.0
WATCHDOG_POLL_SEC = 2.0
TRADE_LOG_INTERVAL = 250


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class LastTrade:
    price: float
    size: float
    side: str
    timestamp: float


class MarketDataFeed:
    """Connects to Polymarket WebSocket and maintains live order book state."""

    def __init__(self, ws_url: str, asset_ids: list[str]):
        self.ws_url = ws_url
        self.asset_ids = asset_ids
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._last_rx_ts: float = 0.0
        self._last_book_ts: float = 0.0
        self._last_book_by_asset: dict[str, float] = {aid: 0.0 for aid in asset_ids}
        self._trade_count = 0

        # State per asset_id
        self.order_books: dict[str, OrderBook] = {
            aid: OrderBook() for aid in asset_ids
        }
        self.last_trades: dict[str, LastTrade] = {}

        # Callbacks
        self._on_book_update: Optional[Callable] = None
        self._on_trade: Optional[Callable] = None

    def on_book_update(self, callback: Callable):
        self._on_book_update = callback

    def on_trade(self, callback: Callable):
        self._on_trade = callback

    async def connect(self):
        """Connect to the WebSocket and start listening."""
        self._running = True
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        while self._running:
            try:
                async with websockets.connect(self.ws_url, ssl=ssl_ctx) as ws:
                    self._ws = ws
                    now = time.monotonic()
                    self._last_rx_ts = now
                    self._last_book_ts = now
                    self._last_book_by_asset = {aid: now for aid in self.asset_ids}
                    log.info("WebSocket connected to %s", self.ws_url)

                    # Subscribe to market channels
                    subscribe_msg = {
                        "assets_ids": self.asset_ids,
                        "asset_ids": self.asset_ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    log.info("Subscribed to assets: %s", self.asset_ids)

                    # Start ping task and message loop
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    watchdog_task = asyncio.create_task(self._watchdog_loop(ws))
                    try:
                        await self._message_loop(ws)
                    finally:
                        ping_task.cancel()
                        watchdog_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await ping_task
                        with contextlib.suppress(asyncio.CancelledError):
                            await watchdog_task
                        self._ws = None

            except websockets.ConnectionClosed as e:
                log.warning("WebSocket disconnected: %s. Reconnecting in 2s...", e)
                await asyncio.sleep(2)
            except Exception as e:
                log.error("WebSocket error: %s. Reconnecting in 5s...", e)
                await asyncio.sleep(5)

    async def _ping_loop(self, ws):
        """Send PING every 10 seconds to keep connection alive."""
        while True:
            try:
                await ws.send("PING")
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def _watchdog_loop(self, ws):
        """Reconnect if the socket goes silent or book updates stall."""
        while True:
            try:
                await asyncio.sleep(WATCHDOG_POLL_SEC)
                now = time.monotonic()
                rx_idle = now - self._last_rx_ts
                if rx_idle > RX_STALE_SEC:
                    log.warning("No market WS messages for %.1fs. Reconnecting...", rx_idle)
                    await ws.close()
                    break

                book_idle = now - self._last_book_ts
                if book_idle > BOOK_STALE_SEC:
                    stale_assets = [
                        aid[:8]
                        for aid, ts in self._last_book_by_asset.items()
                        if now - ts > BOOK_STALE_SEC
                    ]
                    if stale_assets:
                        log.warning(
                            "No book updates for %.1fs on %s. Reconnecting...",
                            book_idle,
                            ",".join(stale_assets),
                        )
                        await ws.close()
                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Watchdog error: %s", e)
                break

    async def _message_loop(self, ws):
        """Process incoming WebSocket messages."""
        async for raw_msg in ws:
            self._last_rx_ts = time.monotonic()
            if raw_msg == "PONG":
                continue

            try:
                msgs = json.loads(raw_msg)
                # Messages can be a single dict or a list
                if isinstance(msgs, dict):
                    msgs = [msgs]

                for msg in msgs:
                    await self._handle_message(msg)

            except json.JSONDecodeError:
                log.debug("Non-JSON message: %s", raw_msg[:100])
            except Exception as e:
                log.error("Error handling message: %s", e)

    async def _handle_message(self, msg: dict):
        """Route message to appropriate handler."""
        event_type = msg.get("event_type", "")

        if event_type == "book":
            self._handle_book_snapshot(msg)
        elif event_type == "price_change":
            self._handle_price_change(msg)
        elif event_type == "last_trade_price":
            self._handle_last_trade(msg)
        elif event_type == "tick_size_change":
            self._handle_tick_size_change(msg)

    def _handle_book_snapshot(self, msg: dict):
        """Process full order book snapshot."""
        asset_id = msg.get("asset_id", "")
        if asset_id not in self.order_books:
            return

        book = self.order_books[asset_id]
        now = time.time()
        mono_now = time.monotonic()

        # Parse bids and asks
        bids_raw = msg.get("bids", [])
        asks_raw = msg.get("asks", [])

        book.bids = sorted(
            [OrderBookLevel(float(b["price"]), float(b["size"])) for b in bids_raw],
            key=lambda x: x.price,
            reverse=True,
        )
        book.asks = sorted(
            [OrderBookLevel(float(a["price"]), float(a["size"])) for a in asks_raw],
            key=lambda x: x.price,
        )
        book.timestamp = now
        self._last_book_ts = mono_now
        self._last_book_by_asset[asset_id] = mono_now

        log.debug(
            "Book snapshot %s: bid=%s ask=%s mid=%s",
            asset_id[:8],
            book.best_bid,
            book.best_ask,
            book.mid_price,
        )

        if self._on_book_update:
            self._on_book_update(asset_id, book)

    def _handle_price_change(self, msg: dict):
        """Process incremental price level update."""
        asset_id = msg.get("asset_id", "")
        if asset_id not in self.order_books:
            return

        book = self.order_books[asset_id]
        price = float(msg.get("price", 0))
        size = float(msg.get("size", 0))
        side = msg.get("side", "").upper()
        now = time.time()
        mono_now = time.monotonic()

        if side == "BUY":
            levels = book.bids
            if size == 0:
                book.bids = [l for l in levels if l.price != price]
            else:
                updated = False
                for l in levels:
                    if l.price == price:
                        l.size = size
                        updated = True
                        break
                if not updated:
                    levels.append(OrderBookLevel(price, size))
                book.bids = sorted(levels, key=lambda x: x.price, reverse=True)
        elif side == "SELL":
            levels = book.asks
            if size == 0:
                book.asks = [l for l in levels if l.price != price]
            else:
                updated = False
                for l in levels:
                    if l.price == price:
                        l.size = size
                        updated = True
                        break
                if not updated:
                    levels.append(OrderBookLevel(price, size))
                book.asks = sorted(levels, key=lambda x: x.price)

        book.timestamp = now
        self._last_book_ts = mono_now
        self._last_book_by_asset[asset_id] = mono_now

        if self._on_book_update:
            self._on_book_update(asset_id, book)

    def _handle_last_trade(self, msg: dict):
        """Process trade execution event."""
        asset_id = msg.get("asset_id", "")
        trade = LastTrade(
            price=float(msg.get("price", 0)),
            size=float(msg.get("size", 0)),
            side=msg.get("side", ""),
            timestamp=time.time(),
        )
        self.last_trades[asset_id] = trade
        self._trade_count += 1
        if self._trade_count % TRADE_LOG_INTERVAL == 0:
            log.info(
                "Trades streamed: %d | last %s %s @ %.4f",
                self._trade_count,
                asset_id[:8],
                trade.side,
                trade.price,
            )
        else:
            log.debug(
                "Trade %s: %s %.2f @ %.4f",
                asset_id[:8],
                trade.side,
                trade.size,
                trade.price,
            )

        if self._on_trade:
            self._on_trade(asset_id, trade)

    def _handle_tick_size_change(self, msg: dict):
        """Log tick size changes."""
        asset_id = msg.get("asset_id", "")
        new_tick = msg.get("new_tick_size", "")
        log.info("Tick size changed for %s: %s", asset_id[:8], new_tick)

    async def disconnect(self):
        """Gracefully disconnect."""
        self._running = False
        if self._ws:
            await self._ws.close()
            log.info("WebSocket disconnected")

    def health(self) -> dict:
        """Summarize feed health for the dashboard."""
        now = time.monotonic()
        rx_age = now - self._last_rx_ts if self._last_rx_ts else None
        book_age = now - self._last_book_ts if self._last_book_ts else None
        stale_assets = []
        if self._last_book_by_asset:
            stale_assets = [
                aid[:8]
                for aid, ts in self._last_book_by_asset.items()
                if ts and (now - ts) > BOOK_STALE_SEC
            ]

        if not self._running:
            status = "offline"
        elif self._ws is None:
            status = "reconnecting"
        elif (rx_age and rx_age > RX_STALE_SEC) or stale_assets:
            status = "stale"
        else:
            status = "live"

        return {
            "status": status,
            "ws_connected": self._ws is not None,
            "rx_age_sec": round(rx_age, 1) if rx_age is not None else None,
            "book_age_sec": round(book_age, 1) if book_age is not None else None,
            "stale_assets": stale_assets,
        }

    def get_book(self, asset_id: str) -> Optional[OrderBook]:
        return self.order_books.get(asset_id)

    def get_mid_price(self, asset_id: str) -> Optional[float]:
        book = self.order_books.get(asset_id)
        if book:
            return book.mid_price
        return None
