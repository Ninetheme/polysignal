"""Risk manager: position tracking, PnL computation, and safety limits."""

import time
from dataclasses import dataclass, field
from typing import Optional

from core.order_manager import OrderSide
from utils.logger import setup_logger

log = setup_logger("risk_mgr")


@dataclass
class Position:
    """Tracks position for a single asset."""
    asset_id: str
    net_contracts: float = 0.0       # positive = long, negative = short
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    total_bought: float = 0.0        # Total contracts bought
    total_sold: float = 0.0          # Total contracts sold
    total_cost: float = 0.0          # Total USDC spent on buys
    total_revenue: float = 0.0       # Total USDC received from sells

    @property
    def unrealized_pnl(self) -> float:
        """Unrealized PnL (requires mark price to be set externally)."""
        return 0.0  # Will be computed by RiskManager with current mid price

    def record_fill(self, side: OrderSide, price: float, size: float):
        """Update position on fill."""
        if side == OrderSide.BUY:
            new_contracts = self.net_contracts + size
            if self.net_contracts >= 0:
                # Adding to long position
                total_cost = self.avg_entry_price * self.net_contracts + price * size
                self.avg_entry_price = total_cost / new_contracts if new_contracts > 0 else 0
            else:
                # Closing short position
                pnl = (self.avg_entry_price - price) * min(size, abs(self.net_contracts))
                self.realized_pnl += pnl
                if new_contracts > 0:
                    self.avg_entry_price = price

            self.net_contracts = new_contracts
            self.total_bought += size
            self.total_cost += price * size

        elif side == OrderSide.SELL:
            new_contracts = self.net_contracts - size
            if self.net_contracts <= 0:
                # Adding to short position
                total_cost = abs(self.avg_entry_price * self.net_contracts) + price * size
                self.avg_entry_price = total_cost / abs(new_contracts) if new_contracts != 0 else 0
            else:
                # Closing long position
                pnl = (price - self.avg_entry_price) * min(size, self.net_contracts)
                self.realized_pnl += pnl
                if new_contracts < 0:
                    self.avg_entry_price = price

            self.net_contracts = new_contracts
            self.total_sold += size
            self.total_revenue += price * size


class RiskManager:
    """Enforces risk limits and tracks portfolio state."""

    def __init__(
        self,
        max_position: float = 100.0,
        max_loss_usdc: float = 50.0,
        max_open_orders: int = 10,
    ):
        self.max_position = max_position
        self.max_loss_usdc = max_loss_usdc
        self.max_open_orders = max_open_orders

        # Positions per asset_id
        self.positions: dict[str, Position] = {}

        # Global halt flag
        self.halted = False
        self.halt_reason: Optional[str] = None

    def get_position(self, asset_id: str) -> Position:
        if asset_id not in self.positions:
            self.positions[asset_id] = Position(asset_id=asset_id)
        return self.positions[asset_id]

    def record_fill(self, asset_id: str, side: OrderSide, price: float, size: float):
        """Record a trade fill and check risk limits."""
        pos = self.get_position(asset_id)
        pos.record_fill(side, price, size)

        log.info(
            "FILL %s %s %.1f @ %.4f | net=%.1f avg=%.4f rpnl=%.2f",
            side.value,
            asset_id[:8],
            size,
            price,
            pos.net_contracts,
            pos.avg_entry_price,
            pos.realized_pnl,
        )

        self._check_limits()

    def compute_unrealized_pnl(self, asset_id: str, mid_price: float) -> float:
        """Compute unrealized PnL for an asset given current mid price."""
        pos = self.get_position(asset_id)
        if pos.net_contracts == 0:
            return 0.0

        if pos.net_contracts > 0:
            return (mid_price - pos.avg_entry_price) * pos.net_contracts
        else:
            return (pos.avg_entry_price - mid_price) * abs(pos.net_contracts)

    def total_pnl(self, mid_prices: dict[str, float]) -> float:
        """Total realized + unrealized PnL across all positions."""
        total = 0.0
        for asset_id, pos in self.positions.items():
            total += pos.realized_pnl
            if asset_id in mid_prices:
                total += self.compute_unrealized_pnl(asset_id, mid_prices[asset_id])
        return total

    def can_place_order(
        self, asset_id: str, side: OrderSide, size: float, num_active_orders: int
    ) -> tuple[bool, str]:
        """Check if a new order is allowed by risk limits."""
        if self.halted:
            return False, f"Trading halted: {self.halt_reason}"

        # Check open order count
        if num_active_orders >= self.max_open_orders:
            return False, f"Max open orders reached ({self.max_open_orders})"

        # Check position limit
        pos = self.get_position(asset_id)
        projected = pos.net_contracts + size if side == OrderSide.BUY else pos.net_contracts - size
        if abs(projected) > self.max_position:
            return False, f"Position limit exceeded: projected={projected:.1f} max={self.max_position}"

        return True, "OK"

    def _check_limits(self):
        """Check if any risk limit is breached and halt if needed."""
        for asset_id, pos in self.positions.items():
            if abs(pos.net_contracts) > self.max_position:
                self._halt(f"Position limit breached for {asset_id[:8]}: {pos.net_contracts:.1f}")
                return

        # Check total realized loss
        total_realized = sum(p.realized_pnl for p in self.positions.values())
        if total_realized < -self.max_loss_usdc:
            self._halt(f"Max loss breached: realized_pnl={total_realized:.2f}")

    def _halt(self, reason: str):
        self.halted = True
        self.halt_reason = reason
        log.error("RISK HALT: %s", reason)

    def reset_halt(self):
        self.halted = False
        self.halt_reason = None
        log.info("Risk halt cleared")

    def get_inventory_skew(self, asset_id: str) -> float:
        """Return net position as a skew factor.

        Returns net_contracts. Positive = long (should lower bid, raise ask).
        Used by QuoteEngine to skew quotes.
        """
        pos = self.get_position(asset_id)
        return pos.net_contracts

    def summary(self, mid_prices: Optional[dict[str, float]] = None) -> str:
        """Human-readable portfolio summary."""
        lines = ["=== Portfolio Summary ==="]
        for asset_id, pos in self.positions.items():
            upnl = 0.0
            if mid_prices and asset_id in mid_prices:
                upnl = self.compute_unrealized_pnl(asset_id, mid_prices[asset_id])
            lines.append(
                f"  {asset_id[:12]}: net={pos.net_contracts:+.1f} "
                f"avg={pos.avg_entry_price:.4f} rpnl={pos.realized_pnl:+.2f} "
                f"upnl={upnl:+.2f}"
            )
        total = self.total_pnl(mid_prices or {})
        lines.append(f"  TOTAL PnL: {total:+.2f} USDC")
        if self.halted:
            lines.append(f"  ** HALTED: {self.halt_reason} **")
        return "\n".join(lines)
