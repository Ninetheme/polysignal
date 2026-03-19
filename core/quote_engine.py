"""Maker-only buy engine: staggered bids with 3% spread between UP/DOWN.

Rules:
- ALL orders are BID (buy limit) — never hit the ask
- 3% spread between UP and DOWN bid prices
- Direction determines priority: strong side gets closer-to-mid bids
- Example (direction=DOWN, mid UP=¢40, DOWN=¢60):
    DOWN bids: ¢59, ¢58, ¢57... (close to mid, priority)
    UP bids:   ¢37, ¢36, ¢35... (3% below DOWN bids)
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from config.settings import QuotingConfig, StrategyFlags
from core.market_data import OrderBook
from core.order_manager import OrderManager, OrderSide, ManagedOrder
from utils.logger import setup_logger

log = setup_logger("quote_eng")


@dataclass
class BuyLevel:
    price: float
    size: float
    order: Optional[ManagedOrder] = None


@dataclass
class BuyPlan:
    levels: list[BuyLevel] = field(default_factory=list)


class QuoteEngine:
    """Maker-only bid engine with 3% UP/DOWN spread."""

    def __init__(self, config: QuotingConfig, order_manager: OrderManager,
                 risk_manager=None, strategy_flags: StrategyFlags = None):
        self.config = config
        self.order_mgr = order_manager
        self.strategy_flags = strategy_flags or StrategyFlags()

        self.active_plans: dict[str, BuyPlan] = {}
        self.quote_count = 0
        self.num_levels = 10
        self.level_spacing_bps = 50.0

        # Timing
        self.window_start_time: float = 0.0
        self._phase1_done = False
        self._phase2_done = False
        self._phase3_done = False
        self._phase4_done = False

    def set_window_start(self, ts: float):
        self.window_start_time = ts
        self._phase1_done = False
        self._phase2_done = False
        self._phase3_done = False
        self._phase4_done = False

    def current_phase(self) -> int:
        if self.window_start_time <= 0:
            return 0
        elapsed = time.time() - self.window_start_time
        if elapsed <= 60:
            return 1
        elif elapsed <= 180:
            return 2
        elif elapsed <= 240:
            return 3
        elif elapsed <= 290:
            return 4
        return 0

    def phase_info(self) -> dict:
        elapsed = time.time() - self.window_start_time if self.window_start_time > 0 else 0
        phase = self.current_phase()
        remaining = {1: 60, 2: 180, 3: 240, 4: 290}.get(phase, 0) - elapsed
        return {
            "phase": phase, "elapsed": round(elapsed, 0),
            "remaining": round(max(0, remaining), 0),
            "p1_done": self._phase1_done, "p2_done": self._phase2_done,
            "p3_done": self._phase3_done, "p4_done": self._phase4_done,
        }

    def should_buy(self, phase: int) -> bool:
        return not getattr(self, f'_phase{phase}_done', True)

    def mark_phase_done(self, phase: int):
        setattr(self, f'_phase{phase}_done', True)

    def compute_maker_bids(self, book: OrderBook, budget: float) -> Optional[BuyPlan]:
        """Place bid orders BELOW best bid (maker only, never cross spread).

        Bids placed from best_bid downward with tick spacing.
        Post-only: must be below best ask to rest on book.
        """
        best_bid = book.best_bid
        best_ask = book.best_ask
        if not best_bid or best_bid <= 0:
            return None

        tick = self.config.tick_size

        # Start at best_bid, space downward
        top_price = best_bid
        spacing = max(tick, best_bid * self.level_spacing_bps / 10000.0)

        # Strict budget tracking — never exceed
        remaining_budget = budget
        levels = []

        for i in range(self.num_levels):
            if remaining_budget < 0.50:  # minimum meaningful order
                break

            price = top_price - i * spacing
            price = round(round(price / tick) * tick, 4)
            if price <= 0 or price >= 1.0:
                continue
            if best_ask and price >= best_ask:
                continue

            # Size from remaining budget, not fixed per-level
            level_budget = remaining_budget / max(1, self.num_levels - i)
            size = round(level_budget / max(price, tick), 1)
            size = max(5.0, size)

            # Double-check: don't exceed remaining
            cost = price * size
            if cost > remaining_budget:
                size = round(remaining_budget / max(price, tick), 1)
                if size < 5.0:
                    break
                cost = price * size

            levels.append(BuyLevel(price=price, size=size))
            remaining_budget -= cost

        return BuyPlan(levels=levels) if levels else None

    async def place_maker_bids(self, asset_id: str, book: OrderBook,
                                budget: float, label: str = "",
                                custom_expiration_sec: int = 0) -> Optional[BuyPlan]:
        """Place maker bid orders on the book."""
        plan = self.compute_maker_bids(book, budget)
        if not plan:
            return None

        await self._cancel_unfilled(asset_id)

        exp_sec = custom_expiration_sec if custom_expiration_sec > 0 else self.config.quote_lifetime_sec

        placed = 0
        total_cost = 0.0
        for level in plan.levels:
            order = await self.order_mgr.place_limit_order(
                asset_id=asset_id, side=OrderSide.BUY,
                price=level.price, size=level.size,
                expiration_sec=exp_sec,
            )
            level.order = order
            if order:
                placed += 1
                total_cost += level.price * level.size

        self.active_plans[asset_id] = plan
        self.quote_count += 1

        elapsed = time.time() - self.window_start_time if self.window_start_time > 0 else 0
        prices = [f"¢{l.price*100:.0f}" for l in plan.levels[:5]]
        log.info(
            "BID %s: %d emir [%s] $%.0f | %ds",
            label, placed, ",".join(prices), total_cost, elapsed,
        )
        return plan

    def check_drift(self, asset_id: str, book: OrderBook) -> float:
        """Check how much orderbook has drifted from our placed bids.

        Returns drift percentage. If >5%, orders should be replaced.
        """
        plan = self.active_plans.get(asset_id)
        if not plan or not plan.levels:
            return 0.0

        best_bid = book.best_bid
        if not best_bid or best_bid <= 0:
            return 0.0

        # Our highest bid price
        active_prices = [l.price for l in plan.levels if l.order and l.order.order_id in self.order_mgr.active_orders]
        if not active_prices:
            return 0.0
        our_top = max(active_prices)
        if our_top <= 0:
            return 0.0

        # Drift = how far our top bid is from current best_bid
        drift = abs(best_bid - our_top) / our_top * 100
        return drift

    def unfilled_budget(self, asset_id: str) -> float:
        """Calculate budget still locked in unfilled orders."""
        plan = self.active_plans.get(asset_id)
        if not plan:
            return 0.0
        total = 0.0
        for l in plan.levels:
            if l.order and l.order.order_id in self.order_mgr.active_orders:
                total += l.price * l.size
        return total

    async def _cancel_unfilled(self, asset_id: str):
        existing = self.active_plans.get(asset_id)
        if not existing:
            return
        for level in existing.levels:
            if level.order and level.order.order_id in self.order_mgr.active_orders:
                await self.order_mgr.cancel_order(level.order.order_id)

    async def cancel_all_quotes(self):
        for asset_id in list(self.active_plans.keys()):
            await self._cancel_unfilled(asset_id)
        self.active_plans.clear()
        log.info("Tum emirler iptal edildi")
