import time
import warnings
import unittest
from unittest.mock import patch

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL 1.1.1+",
)

from config.settings import BotConfig
from dashboard.server import DashboardServer
from core.limit_engine import LimitEngine
from core.market_data import LastTrade, MarketDataFeed, OrderBook, OrderBookLevel
from core.order_manager import ManagedOrder, OrderManager, OrderSide, OrderStatus
from core.paper_engine import PaperPortfolio
from core.signal_tracker import SignalSnapshot, SignalState
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


class FakeSignalTracker:
    def __init__(self, snapshot: SignalSnapshot):
        self.history = [snapshot]


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


class DashboardTests(unittest.TestCase):
    def test_state_returns_last_100_market_history_rows(self):
        strategy = TestStrategy(BotConfig())
        strategy.market_history = [{"resolved_pnl": float(i), "winner": "UP"} for i in range(150)]
        dashboard = DashboardServer(strategy)

        state = dashboard._state()

        self.assertEqual(len(state["market_history"]), 100)
        self.assertEqual(state["market_history"][0]["resolved_pnl"], 50.0)
        self.assertEqual(state["market_history"][-1]["resolved_pnl"], 149.0)


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
        strategy.bot_active = True

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


class PaperPortfolioTests(unittest.TestCase):
    """PaperPortfolio binary PnL hesaplama testleri."""

    def test_balanced_pair_guarantees_profit(self):
        """Esit share aliminda pair cost < $1 ise garanti kar."""
        pf = PaperPortfolio()
        # 100 share UP @ ¢48, 100 share DN @ ¢49 = pair cost ¢97
        pf.up.record_buy(0.48, 100)
        pf.down.record_buy(0.49, 100)
        # Total cost = $48 + $49 = $97
        self.assertAlmostEqual(pf.total_cost(), 97.0, places=1)
        # UP kazanirsa: payout = 100 × $1 = $100, PnL = $100 - $97 = $3
        res = pf.resolve_market("UP")
        self.assertGreater(res["resolved_pnl"], 0)
        # DN kazanirsa da ayni
        res = pf.resolve_market("DOWN")
        self.assertGreater(res["resolved_pnl"], 0)

    def test_imbalanced_pair_creates_loss_risk(self):
        """Dengesiz alim zarar riski olusturur."""
        pf = PaperPortfolio()
        # 200 share UP @ ¢48, 50 share DN @ ¢49 = dengesiz
        pf.up.record_buy(0.48, 200)
        pf.down.record_buy(0.49, 50)
        cost = pf.total_cost()  # $96 + $24.50 = $120.50
        # DN kazanirsa: payout = 50 × $1 = $50, PnL = $50 - $120.50 = -$70.50
        res = pf.resolve_market("DOWN")
        self.assertLess(res["resolved_pnl"], 0)
        # UP kazanirsa: payout = 200 × $1 = $200, PnL = $200 - $120.50 = +$79.50
        res = pf.resolve_market("UP")
        self.assertGreater(res["resolved_pnl"], 0)

    def test_worst_pnl_reflects_weaker_side(self):
        """worst_pnl en zayif senaryoyu gostermeli."""
        pf = PaperPortfolio()
        pf.up.record_buy(0.50, 100)
        pf.down.record_buy(0.50, 30)
        pm = pf.polymarket_pnl()
        # worst = min(100 - 65, 30 - 65) = min(35, -35) = -35
        self.assertLess(pm["worst_pnl"], 0)
        self.assertGreater(pm["best_pnl"], 0)


class RiskControlTests(unittest.IsolatedAsyncioTestCase):
    """Strategy risk kontrol testleri."""

    async def test_check_max_loss_detects_imbalance(self):
        """Butce risk limiti asildiginda agir taraf bloklanmali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._risk_sell_triggered = False
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy.portfolio.up.record_buy(0.60, 250)  # $150 cost, DOWN payout = $0
        result = strategy._check_max_loss()
        self.assertTrue(result)
        self.assertEqual(strategy._cost_block_side, "UP")

    async def test_check_max_loss_ok_when_balanced(self):
        """Dengeli pozisyonda zarar limiti tetiklenmemeli."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._risk_sell_triggered = False
        strategy.portfolio.up.record_buy(0.48, 50)
        strategy.portfolio.down.record_buy(0.49, 50)
        result = strategy._check_max_loss()
        self.assertFalse(result)
        self.assertIsNone(strategy._cost_block_side)


class RebalanceTests(unittest.IsolatedAsyncioTestCase):
    """Directional follow + reversal testleri."""

    async def test_rebalance_targets_reversal_side(self):
        """Reversal hedefi aktifken catch-up emirleri yeni yone gitmeli."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy._strategy_state = "ALIM"
        strategy._active_direction = "DOWN"
        strategy._reversal_target_side = "DOWN"
        strategy._reversal_target_cost = 150.0

        strategy.portfolio.up.record_buy(0.60, 250)   # $150
        strategy.portfolio.down.record_buy(0.50, 50)  # $25

        up_book = OrderBook(
            bids=[OrderBookLevel(0.48, 500)],
            asks=[OrderBookLevel(0.52, 500)],
        )
        dn_book = OrderBook(
            bids=[OrderBookLevel(0.25, 500)],
            asks=[OrderBookLevel(0.26, 500)],
        )

        class FakeMarketFeed:
            def get_book(self, aid):
                return up_book if aid == "up-asset" else dn_book

        strategy.market_feed = FakeMarketFeed()
        strategy.btc_feed = None

        orders_before = strategy.stats["orders_sent"]
        await strategy._rebalance_if_needed(2, 200)
        self.assertGreaterEqual(strategy.stats["orders_sent"], orders_before)
        self.assertTrue(strategy.order_mgr.active_orders)
        self.assertTrue(all(o.asset_id == "down-asset" for o in strategy.order_mgr.active_orders.values()))

    async def test_buy_in_direction_respects_cost_block(self):
        """Risk bloklu degilse secilen yon icin alim yapilmali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy._cost_block_side = "UP"

        up_book = OrderBook(
            bids=[OrderBookLevel(0.48, 500)],
            asks=[OrderBookLevel(0.52, 500)],
        )
        dn_book = OrderBook(
            bids=[OrderBookLevel(0.49, 500)],
            asks=[OrderBookLevel(0.51, 500)],
        )

        class FakeMarketFeed:
            def get_book(self, aid):
                return up_book if aid == "up-asset" else dn_book

        strategy.market_feed = FakeMarketFeed()

        orders_before = strategy.stats["orders_sent"]
        await strategy._buy_in_direction("DOWN", 200)
        self.assertGreater(strategy.stats["orders_sent"], orders_before)
        self.assertGreater(strategy.portfolio.down.shares, 0)
        self.assertEqual(strategy.portfolio.up.shares, 0)

    async def test_buy_in_direction_targets_selected_side_only(self):
        """Directional alim sadece aktif yone emir gondermeli."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy._current_phase = 0

        up_book = OrderBook(
            bids=[OrderBookLevel(0.40, 500)],
            asks=[OrderBookLevel(0.44, 500)],
        )
        dn_book = OrderBook(
            bids=[OrderBookLevel(0.55, 500)],
            asks=[OrderBookLevel(0.58, 500)],
        )

        class FakeMarketFeed:
            def get_book(self, aid):
                return up_book if aid == "up-asset" else dn_book

        strategy.market_feed = FakeMarketFeed()

        await strategy._buy_in_direction("UP", 200)

        self.assertGreater(strategy.portfolio.up.shares, 0)
        self.assertEqual(strategy.portfolio.down.shares, 0)
        self.assertGreater(strategy._phase_spent[0], 0)

    async def test_buy_in_direction_stops_after_orderbook_90(self):
        """Aktif yon orderbook'ta 90 gorurse yeni alim durmali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]

        up_book = OrderBook(
            bids=[OrderBookLevel(0.90, 500)],
            asks=[OrderBookLevel(0.91, 500)],
        )
        dn_book = OrderBook(
            bids=[OrderBookLevel(0.10, 500)],
            asks=[OrderBookLevel(0.11, 500)],
        )

        class FakeMarketFeed:
            def get_book(self, aid):
                return up_book if aid == "up-asset" else dn_book

        strategy.market_feed = FakeMarketFeed()

        await strategy._buy_in_direction("UP", 200)
        self.assertTrue(strategy._orderbook_stop_hit)
        self.assertEqual(strategy._orderbook_stop_side, "UP")
        self.assertFalse(strategy.order_mgr.active_orders)

    async def test_taker_buy_hard_clamps_to_remaining_budget(self):
        """Alt katman taker alimi caller hatasi olsa da kalan butceyi asmamali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 100.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy.portfolio.up.record_buy(0.50, 190)  # $95 cost

        spent = await strategy._taker_buy("DOWN", 1, 0.50, 40.0)

        self.assertAlmostEqual(spent, 5.0, places=2)
        self.assertAlmostEqual(strategy.portfolio.total_cost(), 100.0, places=2)

    async def test_maker_buy_hard_clamps_to_remaining_budget(self):
        """Maker alim de kalan market butcesini gecmemeli."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 100.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy.portfolio.up.record_buy(0.50, 190)  # $95 cost

        dn_book = OrderBook(
            bids=[OrderBookLevel(0.50, 500), OrderBookLevel(0.49, 500)],
            asks=[OrderBookLevel(0.52, 500)],
        )

        class FakeMarketFeed:
            def get_book(self, aid):
                return dn_book

        strategy.market_feed = FakeMarketFeed()

        placed = await strategy._maker_buy("DOWN", 1, 0.50, 40.0)

        self.assertLessEqual(placed, 5.0 + 1e-6)
        self.assertAlmostEqual(strategy.order_mgr.pending_notional(), placed, places=2)

    async def test_buy_cheap_token_under_15_uses_five_pct_budget(self):
        """¢15 alti token icin %5 butceyle taker alim yapilmali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy._current_phase = 0

        up_book = OrderBook(
            bids=[OrderBookLevel(0.13, 500)],
            asks=[OrderBookLevel(0.14, 500)],
        )
        dn_book = OrderBook(
            bids=[OrderBookLevel(0.86, 500)],
            asks=[OrderBookLevel(0.87, 500)],
        )

        class FakeMarketFeed:
            def get_book(self, aid):
                return up_book if aid == "up-asset" else dn_book

        strategy.market_feed = FakeMarketFeed()

        await strategy._buy_cheap_token_if_needed(120)

        self.assertIn("UP", strategy._cheap_take_fired)
        self.assertGreater(strategy.portfolio.up.shares, 0)
        self.assertEqual(strategy.portfolio.down.shares, 0)
        self.assertAlmostEqual(strategy.portfolio.up.total_cost, 50.0, delta=0.1)

    async def test_buy_cheap_token_under_15_fires_once_per_side(self):
        """Ayni markette ayni ucuz taraf icin tekrar tekrar taker atmamalı."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]

        up_book = OrderBook(
            bids=[OrderBookLevel(0.13, 500)],
            asks=[OrderBookLevel(0.14, 500)],
        )
        dn_book = OrderBook(
            bids=[OrderBookLevel(0.86, 500)],
            asks=[OrderBookLevel(0.87, 500)],
        )

        class FakeMarketFeed:
            def get_book(self, aid):
                return up_book if aid == "up-asset" else dn_book

        strategy.market_feed = FakeMarketFeed()

        await strategy._buy_cheap_token_if_needed(120)
        first_cost = strategy.portfolio.up.total_cost
        first_fills = strategy.stats["fills"]

        await strategy._buy_cheap_token_if_needed(120)

        self.assertAlmostEqual(strategy.portfolio.up.total_cost, first_cost, places=2)
        self.assertEqual(strategy.stats["fills"], first_fills)


class CostBalanceTests(unittest.IsolatedAsyncioTestCase):
    """Butce ve reversal hedefi testleri."""

    async def test_worst_case_after_uses_cost_not_budget(self):
        """_worst_case_after yuzdeyi toplam butceye gore vermeli."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._cost_block_side = None
        strategy.portfolio.up.record_buy(0.50, 80)
        strategy.portfolio.down.record_buy(0.50, 120)
        result = strategy._worst_case_after(0, 0, 0, 0)
        self.assertAlmostEqual(result, 2.0, places=1)

    async def test_cost_block_prevents_heavy_side_orders(self):
        """Maliyet bloklama agir tarafa emir gondermeyi engellemeli."""
        strategy = TestStrategy(BotConfig())
        strategy._cost_block_side = "UP"
        self.assertEqual(strategy._cost_block_side, "UP")

    async def test_check_max_loss_blocks_heavy_side_at_10pct(self):
        """%10 butce riski asildiginda agir taraf bloklanmali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._risk_sell_triggered = False
        strategy._cost_rebalance_triggered = False
        strategy._cost_block_side = None
        strategy._current_asset_ids = ["up-asset", "down-asset"]
        strategy.portfolio.up.record_buy(0.75, 200)  # $150 cost, risk > $100
        self.assertTrue(strategy._check_max_loss())
        self.assertEqual(strategy._cost_block_side, "UP")

    async def test_balanced_position_no_block(self):
        """Dengeli pozisyonda bloklama olmamali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._risk_sell_triggered = False
        strategy._cost_rebalance_triggered = False
        strategy._cost_block_side = None
        strategy.portfolio.up.record_buy(0.48, 100)
        strategy.portfolio.down.record_buy(0.49, 100)
        # cost = 48+49 = 97, worst = 100-97 = +3 → kar
        result = strategy._check_max_loss()
        self.assertFalse(result)
        self.assertIsNone(strategy._cost_block_side)

    async def test_arm_reversal_target_matches_previous_side_spend(self):
        """Yon degisiminde yeni taraf eski taraf maliyetini yakalamali."""
        strategy = TestStrategy(BotConfig())
        strategy.portfolio.up.record_buy(0.60, 100)   # $60
        strategy.portfolio.down.record_buy(0.50, 30)  # $15

        strategy._arm_reversal_target("UP", "DOWN")

        self.assertEqual(strategy._reversal_target_side, "DOWN")
        self.assertAlmostEqual(strategy._reversal_target_cost, 60.0, places=2)

    async def test_get_preferred_direction_uses_only_orderbook_signal(self):
        """BTC verisi olsa bile yon sadece signal state'ten gelmeli."""
        strategy = TestStrategy(BotConfig())
        strategy._signal_state = "STRONG_DOWN"
        strategy.btc_feed = type("BtcStub", (), {"current_price": 999999.0})()
        strategy._start_btc_price = 1.0

        self.assertEqual(strategy._get_preferred_direction(), "DOWN")

        strategy._signal_state = "NEUTRAL"
        self.assertIsNone(strategy._get_preferred_direction())

    async def test_get_preferred_direction_fast_tracks_strong_raw_signal(self):
        """Onay beklenirken bile cok guclu ham orderbook sinyali yone donebilmeli."""
        strategy = TestStrategy(BotConfig())
        strategy.signal_tracker = FakeSignalTracker(
            SignalSnapshot(
                ts=time.time(),
                up_mid=0.68,
                dn_mid=0.32,
                spread_pct=34.0,
                raw_signal=SignalState.STRONG_UP,
                confirmed=SignalState.NEUTRAL,
                confidence=0.82,
                phase=0,
                rapid_move=True,
                edge_score=0.14,
                persistence=0.78,
            )
        )

        self.assertEqual(strategy._get_preferred_direction(), "UP")

    async def test_resolve_winner_from_orderbook_uses_higher_mid(self):
        """Market sonucu sadece orderbook mid'e gore secilmeli."""
        strategy = TestStrategy(BotConfig())
        strategy._current_asset_ids = ["up-asset", "down-asset"]

        up_book = OrderBook(
            bids=[OrderBookLevel(0.34, 500)],
            asks=[OrderBookLevel(0.36, 500)],
        )
        dn_book = OrderBook(
            bids=[OrderBookLevel(0.63, 500)],
            asks=[OrderBookLevel(0.65, 500)],
        )

        class FakeMarketFeed:
            last_trades = {}

            def get_book(self, aid):
                return up_book if aid == "up-asset" else dn_book

        strategy.market_feed = FakeMarketFeed()
        self.assertEqual(strategy._resolve_winner_from_orderbook(), "DOWN")

    async def test_resolve_market_records_history_even_without_trades(self):
        """Islem olmayan market de gecmise yazilmali."""
        strategy = TestStrategy(BotConfig())
        strategy._market_question = "No trade market"
        strategy.budget = 1000.0
        strategy._market_budget = 1000.0
        strategy._active_direction = "DOWN"
        strategy._tick_count = 42

        await strategy._resolve_market()

        self.assertEqual(len(strategy.market_history), 1)
        item = strategy.market_history[0]
        self.assertEqual(item["question"], "No trade market")
        self.assertEqual(item["winner"], "DOWN")
        self.assertFalse(item["traded"])
        self.assertEqual(item["total_cost"], 0)
        self.assertEqual(item["resolved_pnl"], 0)
        self.assertEqual(item["ticks"], 42)
        self.assertEqual(item["budget_open"], 1000.0)
        self.assertEqual(item["budget"], 1000.0)

    async def test_market_budget_cap_tracks_dynamic_input(self):
        """UI butcesi degistiginde kalan toplam spend cap'i de aninda degismeli."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._market_budget = 1000.0

        strategy.budget = 5000.0

        self.assertEqual(strategy._budget_cap(), 5000.0)
        self.assertEqual(strategy._remaining_budget(), 5000.0)

    async def test_set_budget_cancels_pending_orders_that_exceed_new_total_cap(self):
        """Yeni butce UP+DOWN+pending toplam tavaninin altina dusurse acik alislar iptal edilmeli."""
        strategy = TestStrategy(BotConfig())
        strategy.bot_active = True
        strategy.budget = 100.0
        strategy._current_asset_ids = ["up-asset", "down-asset"]

        await strategy.order_mgr.place_limit_order(
            asset_id="up-asset",
            side=OrderSide.BUY,
            price=0.50,
            size=100.0,
            expiration_sec=90,
            taker=False,
        )
        self.assertTrue(strategy.order_mgr.active_orders)

        await strategy.handle_command({"action": "set_budget", "budget": 30.0})

        self.assertEqual(strategy.budget, 30.0)
        self.assertFalse(strategy.order_mgr.active_orders)

    async def test_direction_confirm_secs_shortens_for_strong_reversal_signal(self):
        """Cok guclu hizli ters sinyalde 3sn beklemek yerine hizli flip onayi olmali."""
        strategy = TestStrategy(BotConfig())
        strategy.signal_tracker = FakeSignalTracker(
            SignalSnapshot(
                ts=time.time(),
                up_mid=0.30,
                dn_mid=0.70,
                spread_pct=40.0,
                raw_signal=SignalState.STRONG_DOWN,
                confirmed=SignalState.NEUTRAL,
                confidence=0.90,
                phase=0,
                rapid_move=True,
                edge_score=-0.15,
                persistence=0.80,
            )
        )

        self.assertEqual(strategy._direction_confirm_secs("DOWN"), 1.0)

    async def test_planned_budget_grows_with_signal_strength(self):
        """Guclu orderbook sinyali ayni fazda daha agresif butce acmali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._current_phase = 0
        strategy._active_direction = "UP"
        strategy._direction_since = time.time() - 20

        weak = SignalSnapshot(
            ts=time.time(),
            up_mid=0.55,
            dn_mid=0.45,
            spread_pct=12.0,
            raw_signal=SignalState.STRONG_UP,
            confirmed=SignalState.STRONG_UP,
            confidence=0.25,
            phase=0,
            rapid_move=False,
        )
        strong = SignalSnapshot(
            ts=time.time(),
            up_mid=0.70,
            dn_mid=0.30,
            spread_pct=40.0,
            raw_signal=SignalState.STRONG_UP,
            confirmed=SignalState.STRONG_UP,
            confidence=1.0,
            phase=0,
            rapid_move=True,
        )

        strategy.signal_tracker = FakeSignalTracker(weak)
        weak_budget = strategy._planned_budget_for_direction("UP", 1000.0)

        strategy.signal_tracker = FakeSignalTracker(strong)
        strong_budget = strategy._planned_budget_for_direction("UP", 1000.0)

        self.assertGreater(strong_budget, weak_budget)
        self.assertGreater(strong_budget, weak_budget * 3)

    async def test_phase_budget_room_shrinks_as_phase_spend_accumulates(self):
        """Ayni fazda harcama arttikca kalan agresif alim limiti daralmali."""
        strategy = TestStrategy(BotConfig())
        strategy.budget = 1000.0
        strategy._current_phase = 2
        strategy.signal_tracker = FakeSignalTracker(
            SignalSnapshot(
                ts=time.time(),
                up_mid=0.66,
                dn_mid=0.34,
                spread_pct=32.0,
                raw_signal=SignalState.STRONG_UP,
                confirmed=SignalState.STRONG_UP,
                confidence=1.0,
                phase=2,
                rapid_move=False,
            )
        )

        room_before = strategy._phase_budget_room("UP", 1000.0)
        strategy._phase_spent[2] = room_before - 6.0
        room_after = strategy._phase_budget_room("UP", 1000.0)

        self.assertAlmostEqual(room_after, 6.0, places=2)

        strategy._phase_spent[2] = room_before + 1.0
        self.assertEqual(strategy._phase_budget_room("UP", 1000.0), 0.0)


if __name__ == "__main__":
    unittest.main()
