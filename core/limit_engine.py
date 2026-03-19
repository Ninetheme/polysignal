"""Limit order engine: grab top 3 orderbook levels, place, wait 3s, retry.

No strategy flags, no phases. Just raw limit order execution.
Dashboard sends BUY/SELL commands with token + budget.
Engine places orders at best 3 book levels, waits 3s, cancels unfilled, retries.
Max 5-7% loss tracking per position.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from core.market_data import OrderBook
from core.order_manager import OrderManager, OrderSide, ManagedOrder
from core.paper_engine import PaperPortfolio
from utils.logger import setup_logger

log = setup_logger("limit_eng")

MAX_LOSS_PCT = 1.2  # max %1.2 zarar limiti
RETRY_WAIT = 3.0    # 3 saniye bekle, dolmazsa iptal et ve tekrar dene
MAX_RETRIES = 10     # max 10 deneme


@dataclass
class LimitOrder:
    token: str        # "UP" or "DOWN"
    side: str         # "BUY" or "SELL"
    budget: float     # USDC
    filled: float = 0.0
    retries: int = 0
    active: bool = True


class LimitEngine:
    """Executes limit orders by grabbing top 3 orderbook levels.

    Flow:
    1. Dashboard sends: BUY UP $20 or SELL DOWN $15
    2. Engine reads orderbook top 3 levels
    3. Places maker limit orders spread across those 3 levels
    4. Waits 3 seconds
    5. Cancels unfilled orders
    6. Retries with fresh orderbook (up to MAX_RETRIES)
    """

    def __init__(self, order_manager: OrderManager, portfolio: PaperPortfolio):
        self.order_mgr = order_manager
        self.portfolio = portfolio
        self._active_job: Optional[LimitOrder] = None
        self._running = False
        self.history: list[dict] = []  # completed orders
        self._book_getter: Optional[callable] = None  # asset_id -> OrderBook

    def set_book_getter(self, fn):
        """Set callback to fetch fresh orderbook: fn(asset_id) -> OrderBook."""
        self._book_getter = fn

    def _fresh_book(self, asset_id: str, fallback: OrderBook) -> OrderBook:
        """Get fresh orderbook if getter available, else use fallback."""
        if self._book_getter:
            fresh = self._book_getter(asset_id)
            if fresh:
                return fresh
        return fallback

    @property
    def is_busy(self) -> bool:
        return self._active_job is not None and self._active_job.active

    async def execute_buy(self, asset_id: str, token: str, book: OrderBook,
                          budget: float, btc_price: float = 0) -> dict:
        """Execute a BUY order across top 3 bid levels."""
        job = LimitOrder(token=token, side="BUY", budget=budget)
        self._active_job = job
        result = {"token": token, "side": "BUY", "budget": budget,
                  "filled_shares": 0, "filled_cost": 0, "retries": 0, "status": ""}

        for attempt in range(MAX_RETRIES):
            if not job.active:
                break

            remaining = budget - job.filled
            if remaining < 0.50:
                result["status"] = "TAMAMLANDI"
                break

            # Taze orderbook al
            book = self._fresh_book(asset_id, book)

            # Top 3 bid levels — place just below (maker)
            bids = book.bids[:3] if book.bids else []
            if not bids:
                result["status"] = "KITAP BOS"
                break

            # Spread budget across top 3 levels
            orders_placed = []
            per_level = remaining / max(1, len(bids))

            for level in bids:
                price = level.price
                size = round(per_level / max(price, 0.01), 1)
                if size < 5.0:
                    size = 5.0
                cost = price * size
                if cost > remaining:
                    size = round(remaining / max(price, 0.01), 1)
                    if size < 5.0:
                        continue

                order = await self.order_mgr.place_limit_order(
                    asset_id=asset_id, side=OrderSide.BUY,
                    price=price, size=size, expiration_sec=90,
                )
                if order:
                    orders_placed.append((order, price, size))

            if not orders_placed:
                result["status"] = "EMIR KONULAMADI"
                break

            prices_str = ", ".join(f"¢{p*100:.0f}x{s:.0f}" for _, p, s in orders_placed)
            log.info("ALIS %s: %s (deneme %d)", token, prices_str, attempt + 1)

            # Wait 3 seconds for fills
            await asyncio.sleep(RETRY_WAIT)

            # Check fills and cancel unfilled
            # NOT: ledger kaydini _on_trade() yapiyor, burada tekrar yazmiyoruz
            for order, price, size in orders_placed:
                filled_qty = order.filled_size
                # Kismi veya tam dolum varsa result'a yaz
                if filled_qty > 0:
                    job.filled += price * filled_qty
                    result["filled_shares"] += filled_qty
                    result["filled_cost"] += price * filled_qty
                    log.info("DOLUM %s ALIS %.0f pay @ ¢%.0f ($%.2f)",
                             token, filled_qty, price * 100, price * filled_qty)
                # Hala active ise (kismi veya sifir dolum) iptal et
                if order.order_id in self.order_mgr.active_orders:
                    await self.order_mgr.cancel_order(order.order_id)

            job.retries = attempt + 1
            result["retries"] = attempt + 1

        if result["filled_cost"] > 0 and result["status"] == "":
            result["status"] = "KISMI" if job.filled < budget * 0.9 else "TAMAMLANDI"
        elif result["status"] == "":
            result["status"] = "BASARISIZ"

        self._active_job = None
        self.history.append(result)
        return result

    async def execute_sell(self, asset_id: str, token: str, book: OrderBook,
                           shares: float, btc_price: float = 0) -> dict:
        """Execute a SELL order across top 3 ask levels."""
        job = LimitOrder(token=token, side="SELL", budget=0)
        self._active_job = job
        result = {"token": token, "side": "SELL", "shares_target": shares,
                  "filled_shares": 0, "filled_revenue": 0, "retries": 0, "status": ""}

        for attempt in range(MAX_RETRIES):
            if not job.active:
                break

            remaining_shares = shares - result["filled_shares"]
            if remaining_shares < 5.0:
                result["status"] = "TAMAMLANDI"
                break

            # Taze orderbook al
            book = self._fresh_book(asset_id, book)

            # Top 3 ask levels — place just above (maker)
            asks = book.asks[:3] if book.asks else []
            if not asks:
                result["status"] = "KITAP BOS"
                break

            orders_placed = []
            per_level = remaining_shares / max(1, len(asks))

            for level in asks:
                price = level.price
                size = round(min(per_level, remaining_shares), 1)
                if size < 5.0:
                    size = 5.0
                if size > remaining_shares:
                    size = round(remaining_shares, 1)

                order = await self.order_mgr.place_limit_order(
                    asset_id=asset_id, side=OrderSide.SELL,
                    price=price, size=size, expiration_sec=90,
                )
                if order:
                    orders_placed.append((order, price, size))
                    remaining_shares -= size

            if not orders_placed:
                result["status"] = "EMIR KONULAMADI"
                break

            prices_str = ", ".join(f"¢{p*100:.0f}x{s:.0f}" for _, p, s in orders_placed)
            log.info("SATIS %s: %s (deneme %d)", token, prices_str, attempt + 1)

            # Wait 3 seconds
            await asyncio.sleep(RETRY_WAIT)

            # Check fills
            # NOT: ledger kaydini _on_trade() yapiyor, burada tekrar yazmiyoruz
            for order, price, size in orders_placed:
                filled_qty = order.filled_size
                # Kismi veya tam dolum varsa result'a yaz
                if filled_qty > 0:
                    result["filled_shares"] += filled_qty
                    result["filled_revenue"] += price * filled_qty
                    log.info("DOLUM %s SATIS %.0f pay @ ¢%.0f ($%.2f)",
                             token, filled_qty, price * 100, price * filled_qty)
                # Hala active ise (kismi veya sifir dolum) iptal et
                if order.order_id in self.order_mgr.active_orders:
                    await self.order_mgr.cancel_order(order.order_id)

            job.retries = attempt + 1
            result["retries"] = attempt + 1

        if result["filled_shares"] > 0 and result["status"] == "":
            result["status"] = "KISMI" if result["filled_shares"] < shares * 0.9 else "TAMAMLANDI"
        elif result["status"] == "":
            result["status"] = "BASARISIZ"

        self._active_job = None
        self.history.append(result)
        return result

    async def quick_buy(self, asset_id: str, token: str, book: OrderBook,
                        budget: float, btc_price: float = 0) -> Optional[ManagedOrder]:
        """Tek limit emir koy — beklemeden, agent her saniye cagirir.

        Best bid fiyatindan tek bir maker emir.
        Dolum takibi _on_trade callback ile yapilir.
        """
        if not book or not book.bids:
            return None
        price = book.best_bid
        if not price or price <= 0 or price >= 1.0:
            return None

        size = round(budget / max(price, 0.01), 1)
        if size < 5.0:
            return None

        cost = price * size
        if cost > budget * 1.1:
            size = max(5.0, round(budget / max(price, 0.01), 1))

        order = await self.order_mgr.place_limit_order(
            asset_id=asset_id, side=OrderSide.BUY,
            price=price, size=size, expiration_sec=90)
        return order

    def check_loss_limit(self, up_price: float = 0, dn_price: float = 0) -> dict:
        """Polymarket binary K/Z ile zarar limiti kontrolu.

        worst_pnl = en kotu senaryo (UP veya DOWN kazanir)
        Zarar = |worst_pnl| / total_cost * 100
        """
        pm = self.portfolio.polymarket_pnl()
        cost = pm["total_cost"]
        if cost <= 0:
            return {"exceeded": False, "loss_pct": 0, "worst_pnl": 0}
        worst = pm["worst_pnl"]
        loss_pct = abs(min(0, worst)) / cost * 100
        return {
            "exceeded": loss_pct >= MAX_LOSS_PCT,
            "loss_pct": round(loss_pct, 2),
            "worst_pnl": round(worst, 4),
            "best_pnl": round(pm["best_pnl"], 4),
            "max_loss_pct": MAX_LOSS_PCT,
        }

    def cancel_active(self):
        """Cancel the currently running job."""
        if self._active_job:
            self._active_job.active = False
