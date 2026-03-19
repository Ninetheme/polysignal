import warnings
import unittest
from unittest.mock import patch

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL 1.1.1+",
)

from config.settings import BotConfig
from core.limit_engine import LimitEngine
from core.market_data import LastTrade, MarketDataFeed, OrderBook, OrderBookLevel
from core.order_manager import ManagedOrder, OrderManager, OrderSide, OrderStatus
from core.paper_engine import PaperPortfolio
from core.strategy import LimitBotStrategy


class TestStrategy(LimitBotStrategy):
    def _load_archive(self) -> list[dict]:
        return []

    def _save_archive(self):
        return None

    def _load_history(self) -> list[dict]:
        return []

    def _save_history(self):
        return None


class FakeOrderManager:
    def __init__(self, buy_fill_qty: float = 0.0, sell_fill_qty: float = 0.0):
        self.active_orders: dict[str, ManagedOrder] = {}
        self.buy_fill_qty = buy_fill_qty
        self.sell_fill_qty = sell_fill_qty
        self._counter = 0

    async def place_limit_order(
        self,
        asset_id: str,
        side: OrderSide,
        price: float,
        size: float,
        expiration_sec: int = 90,
    ) -> ManagedOrder:
        del expiration_sec
        self._counter += 1
        order = ManagedOrder(
            order_id=f"TEST-{self._counter}",
            asset_id=asset_id,
            side=side,
            price=price,
            size=size,
            status=OrderStatus.LIVE,
            expiration=9999999999,
        )
        if side == OrderSide.BUY and self.buy_fill_qty > 0:
            order.filled_size = min(size, self.buy_fill_qty)
        if side == OrderSide.SELL and self.sell_fill_qty > 0:
            order.filled_size = min(size, self.sell_fill_qty)
        self.active_orders[order.order_id] = order
        return order

    async def cancel_order(self, order_id: str) -> bool:
        self.active_orders.pop(order_id, None)
        return True


class OrderManagerTests(unittest.TestCase):
    def test_pending_notional_counts_only_unfilled_live_buys(self):
        manager = OrderManager(clob_client=None, test_mode=True, tick_size=0.01)
        manager.active_orders = {
            "buy-live": ManagedOrder(
                order_id="buy-live",
                asset_id="UP",
                side=OrderSide.BUY,
                price=0.42,
                size=10.0,
                status=OrderStatus.LIVE,
                filled_size=3.0,
            ),
            "buy-cancelled": ManagedOrder(
                order_id="buy-cancelled",
                asset_id="UP",
                side=OrderSide.BUY,
                price=0.50,
                size=5.0,
                status=OrderStatus.CANCELLED,
            ),
            "sell-live": ManagedOrder(
                order_id="sell-live",
                asset_id="UP",
                side=OrderSide.SELL,
                price=0.55,
                size=8.0,
                status=OrderStatus.LIVE,
            ),
        }

        self.assertAlmostEqual(manager.pending_notional(), 0.42 * 7.0)


class MarketDataFeedTests(unittest.TestCase):
    def test_sell_price_change_keeps_asks_sorted_ascending(self):
        feed = MarketDataFeed("wss://example.test/ws", ["asset-1"])
        feed._handle_book_snapshot(
            {
                "asset_id": "asset-1",
                "bids": [{"price": "0.45", "size": "10"}],
                "asks": [
                    {"price": "0.60", "size": "5"},
                    {"price": "0.58", "size": "7"},
                ],
            }
        )

        feed._handle_price_change(
            {"asset_id": "asset-1", "side": "SELL", "price": "0.59", "size": "3"}
        )

        book = feed.get_book("asset-1")
        self.assertIsNotNone(book)
        self.assertEqual([level.price for level in book.asks], [0.58, 0.59, 0.60])
        self.assertEqual(book.best_ask, 0.58)


class StrategyFillTests(unittest.TestCase):
    def test_on_trade_respects_taker_side_price_priority_and_partial_fills(self):
        strategy = TestStrategy(BotConfig())
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy.btc_feed = type("BtcStub", (), {"current_price": 0.0})()

        best_buy = ManagedOrder(
            order_id="buy-1",
            asset_id="up-asset",
            side=OrderSide.BUY,
            price=0.55,
            size=5.0,
            status=OrderStatus.LIVE,
        )
        second_buy = ManagedOrder(
            order_id="buy-2",
            asset_id="up-asset",
            side=OrderSide.BUY,
            price=0.54,
            size=5.0,
            status=OrderStatus.LIVE,
        )
        resting_sell = ManagedOrder(
            order_id="sell-1",
            asset_id="up-asset",
            side=OrderSide.SELL,
            price=0.56,
            size=5.0,
            status=OrderStatus.LIVE,
        )
        strategy.order_mgr.active_orders = {
            best_buy.order_id: best_buy,
            second_buy.order_id: second_buy,
            resting_sell.order_id: resting_sell,
        }

        strategy._on_trade(
            "up-asset",
            LastTrade(price=0.54, size=7.0, side="SELL", timestamp=0.0),
        )

        self.assertAlmostEqual(strategy.portfolio.up.shares, 7.0)
        self.assertEqual(strategy.portfolio.down.shares, 0.0)
        self.assertEqual(strategy.stats["fills"], 2)
        self.assertNotIn("buy-1", strategy.order_mgr.active_orders)
        self.assertIn("buy-2", strategy.order_mgr.active_orders)
        self.assertIn("sell-1", strategy.order_mgr.active_orders)
        self.assertAlmostEqual(strategy.order_mgr.active_orders["buy-2"].filled_size, 2.0)
        self.assertEqual(len(strategy.order_archive), 2)
        self.assertAlmostEqual(sum(fill["size"] for fill in strategy.order_archive), 7.0)


class LimitEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_buy_tracks_partial_fill_before_cancel(self):
        order_mgr = FakeOrderManager(buy_fill_qty=4.0)
        engine = LimitEngine(order_mgr, PaperPortfolio())
        book = OrderBook(bids=[OrderBookLevel(price=0.50, size=20.0)])

        with patch("core.limit_engine.RETRY_WAIT", 0), patch("core.limit_engine.MAX_RETRIES", 1):
            result = await engine.execute_buy("asset-1", "UP", book, budget=10.0)

        self.assertEqual(result["status"], "KISMI")
        self.assertAlmostEqual(result["filled_shares"], 4.0)
        self.assertAlmostEqual(result["filled_cost"], 2.0)
        self.assertEqual(order_mgr.active_orders, {})

    async def test_execute_sell_tracks_partial_fill_before_cancel(self):
        order_mgr = FakeOrderManager(sell_fill_qty=3.0)
        engine = LimitEngine(order_mgr, PaperPortfolio())
        book = OrderBook(asks=[OrderBookLevel(price=0.60, size=20.0)])

        with patch("core.limit_engine.RETRY_WAIT", 0), patch("core.limit_engine.MAX_RETRIES", 1):
            result = await engine.execute_sell("asset-1", "UP", book, shares=6.0)

        self.assertEqual(result["status"], "KISMI")
        self.assertAlmostEqual(result["filled_shares"], 3.0)
        self.assertAlmostEqual(result["filled_revenue"], 1.8)
        self.assertEqual(order_mgr.active_orders, {})


if __name__ == "__main__":
    unittest.main()
