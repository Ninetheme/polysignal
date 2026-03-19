"""WebSocket-based market data handler for Polymarket CLOB."""

import asyncio
import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import certifi
import websockets

from utils.logger import setup_logger

log = setup_logger("market_data")


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
                    try:
                        await self._message_loop(ws)
                    finally:
                        ping_task.cancel()

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

    async def _message_loop(self, ws):
        """Process incoming WebSocket messages."""
        async for raw_msg in ws:
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

        log.info(
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

    def get_book(self, asset_id: str) -> Optional[OrderBook]:
        return self.order_books.get(asset_id)

    def get_mid_price(self, asset_id: str) -> Optional[float]:
        book = self.order_books.get(asset_id)
        if book:
            return book.mid_price
        return None
