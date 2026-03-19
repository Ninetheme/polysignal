"""Order management: placement, cancellation, and tracking via Polymarket CLOB API."""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from utils.logger import setup_logger

log = setup_logger("order_mgr")


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    LIVE = "LIVE"
    MATCHED = "MATCHED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass
class ManagedOrder:
    """Internal representation of a managed order."""
    order_id: str
    asset_id: str
    side: OrderSide
    price: float
    size: float
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = field(default_factory=time.time)
    expiration: int = 0  # Unix timestamp
    filled_size: float = 0.0


class OrderManager:
    """Manages order lifecycle with Polymarket CLOB API.

    In test mode, simulates order placement and tracking without
    hitting the real API.
    """

    def __init__(self, clob_client, test_mode: bool = True, tick_size: float = 0.01):
        self._client = clob_client
        self.test_mode = test_mode
        self.tick_size = tick_size

        # Active orders by order_id
        self.active_orders: dict[str, ManagedOrder] = {}
        self._order_counter = 0

        # Heartbeat state
        self._heartbeat_id = ""
        self._heartbeat_running = False

    def _round_price(self, price: float) -> float:
        """Round price to the nearest tick size."""
        return round(round(price / self.tick_size) * self.tick_size, 4)

    async def place_limit_order(
        self,
        asset_id: str,
        side: OrderSide,
        price: float,
        size: float,
        expiration_sec: int = 90,
        taker: bool = False,
    ) -> Optional[ManagedOrder]:
        """Place a limit order.

        Args:
            asset_id: Token ID to trade
            side: BUY or SELL
            price: Limit price (will be rounded to tick size)
            size: Number of contracts
            expiration_sec: Seconds until order expires (minimum 61)
            taker: True = FOK taker emir (aninda dolum), False = GTD maker emir
        """
        price = self._round_price(price)

        # Validate
        if price <= 0 or price >= 1.0:
            log.warning("Invalid price %.4f, skipping", price)
            return None
        if size < 5.0:
            log.warning("Size %.2f below minimum 5, skipping", size)
            return None

        # Expiration must be > 60s from now (Polymarket security threshold)
        expiration_ts = int(time.time()) + max(expiration_sec, 61)

        if self.test_mode:
            return self._simulate_place(asset_id, side, price, size, expiration_ts, taker)

        return await self._real_place(asset_id, side, price, size, expiration_ts, taker)

    def _simulate_place(
        self, asset_id: str, side: OrderSide, price: float, size: float,
        expiration: int, taker: bool = False,
    ) -> ManagedOrder:
        """Simulate order placement in test mode."""
        self._order_counter += 1
        tag = "FOK" if taker else "GTD"
        order_id = f"SIM-{tag}-{self._order_counter:06d}"

        order = ManagedOrder(
            order_id=order_id,
            asset_id=asset_id,
            side=side,
            price=price,
            size=size,
            status=OrderStatus.LIVE,
            expiration=expiration,
            # Taker emir simülasyonda aninda dolmus sayilir
            filled_size=size if taker else 0.0,
        )

        if taker:
            # FOK: aninda doldur, active_orders'a ekleme (zaten bitti)
            order.status = OrderStatus.MATCHED
            log.info(
                "[SIM] TAKER %s %s %.1f @ %.4f id=%s",
                side.value, asset_id[:8], size, price, order_id,
            )
        else:
            self.active_orders[order_id] = order
            log.info(
                "[SIM] MAKER %s %s %.1f @ %.4f (exp=%ds) id=%s",
                side.value, asset_id[:8], size, price,
                expiration - int(time.time()), order_id,
            )
        return order

    async def _real_place(
        self, asset_id: str, side: OrderSide, price: float, size: float,
        expiration: int, taker: bool = False,
    ) -> Optional[ManagedOrder]:
        """Place a real order via the CLOB client."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            clob_side = BUY if side == OrderSide.BUY else SELL

            order_args = OrderArgs(
                token_id=asset_id,
                price=price,
                size=size,
                side=clob_side,
                expiration=str(expiration),
            )

            signed_order = self._client.create_order(order_args)
            # FOK = taker (aninda doldur veya iptal), GTD = maker (bekle)
            order_type = OrderType.FOK if taker else OrderType.GTD
            resp = self._client.post_order(signed_order, order_type)

            if not resp or not resp.get("success"):
                error_msg = resp.get("errorMsg", "Unknown error") if resp else "No response"
                log.error("Order failed (%s): %s", "FOK" if taker else "GTD", error_msg)
                return None

            order_id = resp["orderID"]
            status = OrderStatus.MATCHED if taker else OrderStatus.LIVE
            order = ManagedOrder(
                order_id=order_id,
                asset_id=asset_id,
                side=side,
                price=price,
                size=size,
                status=status,
                expiration=expiration,
                filled_size=size if taker else 0.0,
            )
            if not taker:
                self.active_orders[order_id] = order

            tag = "TAKER" if taker else "MAKER"
            log.info(
                "%s %s %s %.1f @ %.4f id=%s",
                tag, side.value, asset_id[:8], size, price, order_id[:12],
            )
            return order

        except Exception as e:
            log.error("Order error: %s", e)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order."""
        order = self.active_orders.get(order_id)
        if not order or order.status != OrderStatus.LIVE:
            return False

        if self.test_mode:
            order.status = OrderStatus.CANCELLED
            del self.active_orders[order_id]
            log.info("[SIM] CANCELLED %s", order_id)
            return True

        try:
            self._client.cancel(order_id)
            order.status = OrderStatus.CANCELLED
            del self.active_orders[order_id]
            log.info("CANCELLED %s", order_id[:12])
            return True
        except Exception as e:
            log.error("Cancel error for %s: %s", order_id[:12], e)
            return False

    async def cancel_all(self) -> int:
        """Cancel all active orders. Returns count of cancelled orders."""
        if not self.active_orders:
            return 0

        order_ids = list(self.active_orders.keys())

        if self.test_mode:
            for oid in order_ids:
                self.active_orders[oid].status = OrderStatus.CANCELLED
            self.active_orders.clear()
            log.info("[SIM] CANCELLED ALL (%d orders)", len(order_ids))
            return len(order_ids)

        try:
            self._client.cancel_all()
            count = len(order_ids)
            for oid in order_ids:
                self.active_orders[oid].status = OrderStatus.CANCELLED
            self.active_orders.clear()
            log.info("CANCELLED ALL (%d orders)", count)
            return count
        except Exception as e:
            log.error("Cancel all error: %s", e)
            return 0

    def expire_stale_orders(self):
        """Remove orders that have passed their expiration time."""
        now = int(time.time())
        expired = []
        for oid, order in list(self.active_orders.items()):
            if order.expiration > 0 and now >= order.expiration:
                order.status = OrderStatus.EXPIRED
                expired.append(oid)

        for oid in expired:
            del self.active_orders[oid]

        if expired:
            log.info("Expired %d stale orders", len(expired))
        return len(expired)

    def get_live_orders(self, asset_id: Optional[str] = None) -> list[ManagedOrder]:
        """Get all live orders, optionally filtered by asset_id."""
        orders = [o for o in self.active_orders.values() if o.status == OrderStatus.LIVE]
        if asset_id:
            orders = [o for o in orders if o.asset_id == asset_id]
        return orders

    def pending_notional(self) -> float:
        """Total notional of unfilled portions of live BUY orders."""
        total = 0.0
        for o in self.active_orders.values():
            if o.status == OrderStatus.LIVE and o.side == OrderSide.BUY:
                unfilled = o.size - o.filled_size
                if unfilled > 0:
                    total += o.price * unfilled
        return total

    async def start_heartbeat(self, interval: float = 5.0):
        """Start sending heartbeats to keep orders alive (production only)."""
        if self.test_mode:
            log.info("[SIM] Heartbeat not needed in test mode")
            return

        self._heartbeat_running = True
        while self._heartbeat_running:
            try:
                # The heartbeat endpoint expects the previous heartbeat_id
                resp = self._client.heartbeat(self._heartbeat_id)
                if resp and "heartbeat_id" in resp:
                    self._heartbeat_id = resp["heartbeat_id"]
                log.debug("Heartbeat sent: %s", self._heartbeat_id[:8] if self._heartbeat_id else "init")
            except Exception as e:
                log.error("Heartbeat error: %s", e)
            await asyncio.sleep(interval)

    def stop_heartbeat(self):
        self._heartbeat_running = False
