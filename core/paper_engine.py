"""Paper trading engine — pixel-perfect accounting with real market data.

No simulated fills. Instead, watches the REAL Polymarket orderbook and
trade stream tick-by-tick. When a real trade occurs at a price that would
fill our resting limit order, we record it as filled.

All math uses integer cents (1 USDC = 1_000_000 units) to avoid
floating point errors.

K/Z (PnL) = Toplam Gelir - Toplam Maliyet + Envanter Degeri
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import setup_logger

log = setup_logger("paper")

# Use integer accounting: 1 USDC = 1_000_000 micro-USDC
SCALE = 1_000_000


def to_int(x: float) -> int:
    """Convert float USDC to integer micro-USDC."""
    return round(x * SCALE)


def to_float(x: int) -> float:
    """Convert integer micro-USDC to float."""
    return x / SCALE


@dataclass
class PaperFill:
    """A single executed trade."""
    ts: float
    token: str          # "UP" or "DOWN"
    side: str           # "ALIS" or "SATIS"
    price: float        # 0.0 - 1.0
    shares: float       # number of contracts
    cost_usdc: float    # price * shares (USDC spent or received)
    btc_price: float    # BTC/USD at time of fill
    is_seed: bool       # initial inventory purchase


@dataclass
class TokenLedger:
    """Double-entry ledger for one token (UP or DOWN).

    All values in micro-USDC (integer) to prevent floating point drift.
    """
    label: str  # "UP" or "DOWN"

    # Shares held
    shares: float = 0.0

    # Cash flows (integer micro-USDC for precision)
    total_cost_i: int = 0       # total USDC spent buying
    total_revenue_i: int = 0    # total USDC received selling
    seed_cost_i: int = 0        # cost of initial seed (included in total_cost)

    # Counters
    bought: float = 0.0
    sold: float = 0.0
    num_buys: int = 0
    num_sells: int = 0

    # Fill history
    fills: list[PaperFill] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return to_float(self.total_cost_i)

    @property
    def total_revenue(self) -> float:
        return to_float(self.total_revenue_i)

    @property
    def seed_cost(self) -> float:
        return to_float(self.seed_cost_i)

    @property
    def net_cash(self) -> float:
        """Net cash: revenue - cost. Negative = we've spent more than earned."""
        return to_float(self.total_revenue_i - self.total_cost_i)

    def avg_buy_price(self) -> float:
        if self.bought == 0:
            return 0.0
        return self.total_cost / self.bought

    def avg_sell_price(self) -> float:
        if self.sold == 0:
            return 0.0
        return self.total_revenue / self.sold

    def record_buy(self, price: float, shares: float, btc_price: float = 0, seed: bool = False):
        cost_i = to_int(price * shares)
        self.shares += shares
        self.bought += shares
        self.total_cost_i += cost_i
        self.num_buys += 1
        if seed:
            self.seed_cost_i += cost_i

        fill = PaperFill(
            ts=time.time(), token=self.label, side="ALIS",
            price=price, shares=shares, cost_usdc=to_float(cost_i),
            btc_price=btc_price, is_seed=seed,
        )
        self.fills.append(fill)
        return fill

    def record_sell(self, price: float, shares: float, btc_price: float = 0):
        revenue_i = to_int(price * shares)
        self.shares -= shares
        self.sold += shares
        self.total_revenue_i += revenue_i
        self.num_sells += 1

        fill = PaperFill(
            ts=time.time(), token=self.label, side="SATIS",
            price=price, shares=shares, cost_usdc=to_float(revenue_i),
            btc_price=btc_price, is_seed=False,
        )
        self.fills.append(fill)
        return fill

    def mark_to_market(self, current_price: float) -> float:
        """Current value of held shares at given price."""
        return current_price * self.shares

    def unrealized_pnl(self, current_price: float) -> float:
        """Unrealized PnL = mark-to-market + net_cash - 0 (we start with 0).

        Actually: total portfolio value = cash_in_hand + shares_value
        We started with -total_cost (spent cash) and +total_revenue (got cash back)
        So: PnL = (revenue - cost) + shares * current_price
        """
        return self.net_cash + self.mark_to_market(current_price)


class PaperPortfolio:
    """Portfolio of UP + DOWN tokens with correct PnL accounting.

    PnL formula (no avg_entry nonsense):
        K/Z = (UP gelir - UP maliyet + UP envanter * UP fiyat)
            + (DOWN gelir - DOWN maliyet + DOWN envanter * DOWN fiyat)

    This is mathematically identical to:
        K/Z = Toplam_Gelir - Toplam_Maliyet + Toplam_Envanter_Degeri
    """

    def __init__(self):
        self.up = TokenLedger(label="UP")
        self.down = TokenLedger(label="DOWN")
        self.budget: float = 0.0
        self._halted = False
        self._halt_reason: str = ""

    def seed(self, up_price: float, up_shares: float,
             down_price: float, down_shares: float, btc_price: float = 0):
        """Initial inventory purchase."""
        self.up.record_buy(up_price, up_shares, btc_price, seed=True)
        self.down.record_buy(down_price, down_shares, btc_price, seed=True)

        total_seed = self.up.seed_cost + self.down.seed_cost
        log.info(
            "SEED: %s %.0f pay @ ¢%.0f ($%.2f) + %s %.0f pay @ ¢%.0f ($%.2f) = $%.2f",
            "YUKARI", up_shares, up_price * 100, self.up.seed_cost,
            "ASAGI", down_shares, down_price * 100, self.down.seed_cost,
            total_seed,
        )

    def total_pnl(self, up_price: float, down_price: float) -> float:
        """Total PnL across both tokens.

        = (UP revenue - UP cost + UP shares * UP price)
        + (DOWN revenue - DOWN cost + DOWN shares * DOWN price)
        """
        return self.up.unrealized_pnl(up_price) + self.down.unrealized_pnl(down_price)

    def realized_pnl(self) -> float:
        """Realized PnL = net cash only (ignoring open positions)."""
        return self.up.net_cash + self.down.net_cash

    def total_cost(self) -> float:
        return self.up.total_cost + self.down.total_cost

    def total_revenue(self) -> float:
        return self.up.total_revenue + self.down.total_revenue

    def net_outlay(self) -> float:
        """Open cash tied up in inventory after realized sale proceeds."""
        return max(0.0, self.total_cost() - self.total_revenue())

    def total_shares(self) -> float:
        return self.up.shares + self.down.shares

    def polymarket_pnl(self) -> dict:
        """Polymarket binary outcome K/Z hesabi.

        Kazanan her pay = $1, kaybeden = $0.
        UP kazanirsa: gelir = UP_shares * $1 + UP_satis_geliri
        DOWN kazanirsa: gelir = DOWN_shares * $1 + DOWN_satis_geliri

        Gercek K/Z = gelir - toplam_maliyet
        """
        up_cost = self.up.total_cost
        dn_cost = self.down.total_cost
        up_rev = self.up.total_revenue  # satis gelirleri
        dn_rev = self.down.total_revenue
        total_cost = up_cost + dn_cost
        total_rev = up_rev + dn_rev

        # UP kazanirsa: UP paylari $1 eder, DOWN paylari $0
        up_wins_payout = self.up.shares * 1.0
        up_wins_pnl = (up_wins_payout + total_rev) - total_cost

        # DOWN kazanirsa: DOWN paylari $1 eder, UP paylari $0
        dn_wins_payout = self.down.shares * 1.0
        dn_wins_pnl = (dn_wins_payout + total_rev) - total_cost

        # Mevcut (market fiyatina gore - referans)
        # En kotusu = min(up_wins, dn_wins)
        worst_pnl = min(up_wins_pnl, dn_wins_pnl)
        best_pnl = max(up_wins_pnl, dn_wins_pnl)

        return {
            "up_wins_pnl": round(up_wins_pnl, 4),
            "dn_wins_pnl": round(dn_wins_pnl, 4),
            "worst_pnl": round(worst_pnl, 4),
            "best_pnl": round(best_pnl, 4),
            "total_cost": round(total_cost, 4),
            "total_revenue": round(total_rev, 4),
            "up_payout": round(up_wins_payout, 4),
            "dn_payout": round(dn_wins_payout, 4),
        }

    def state(self, up_price: float = 0, down_price: float = 0) -> dict:
        """Full state for dashboard — Polymarket binary K/Z."""
        pm = self.polymarket_pnl()
        return {
            "up_shares": self.up.shares,
            "up_bought": self.up.bought,
            "up_sold": self.up.sold,
            "up_cost": self.up.total_cost,
            "up_revenue": self.up.total_revenue,
            "up_avg_buy": round(self.up.avg_buy_price(), 4),
            "up_payout": pm["up_payout"],  # UP kazanirsa alinan $

            "down_shares": self.down.shares,
            "down_bought": self.down.bought,
            "down_sold": self.down.sold,
            "down_cost": self.down.total_cost,
            "down_revenue": self.down.total_revenue,
            "down_avg_buy": round(self.down.avg_buy_price(), 4),
            "dn_payout": pm["dn_payout"],  # DOWN kazanirsa alinan $

            # Polymarket binary K/Z
            "up_wins_pnl": pm["up_wins_pnl"],    # UP kazanirsa K/Z
            "dn_wins_pnl": pm["dn_wins_pnl"],    # DOWN kazanirsa K/Z
            "worst_pnl": pm["worst_pnl"],         # en kotu senaryo
            "best_pnl": pm["best_pnl"],           # en iyi senaryo
            "total_cost": pm["total_cost"],
            "total_revenue": pm["total_revenue"],
            "total_pnl": pm["worst_pnl"],         # dashboard icin en kotu goster
            "realized_pnl": pm["worst_pnl"],       # binary worst case K/Z
            "total_shares": self.total_shares(),

            "halted": self._halted,
            "halt_reason": self._halt_reason,
        }

    def check_limits(self, max_position: float, max_loss: float,
                     up_price: float, down_price: float):
        """Check risk limits."""
        if abs(self.up.shares) > max_position or abs(self.down.shares) > max_position:
            self._halted = True
            self._halt_reason = f"Pozisyon limiti: UP={self.up.shares:.0f} DOWN={self.down.shares:.0f}"

        pnl = self.total_pnl(up_price, down_price)
        if pnl < -max_loss:
            self._halted = True
            self._halt_reason = f"Zarar limiti: K/Z={pnl:.2f}"

    def all_fills(self) -> list[dict]:
        """All fills from both tokens, sorted by time, for dashboard."""
        all_f = []
        for f in self.up.fills + self.down.fills:
            all_f.append({
                "ts": f.ts,
                "token": f.token,
                "side": f.side,
                "price": f.price,
                "shares": f.shares,
                "cost": f.cost_usdc,
                "btc_price": f.btc_price,
                "seed": f.is_seed,
            })
        all_f.sort(key=lambda x: x["ts"])
        return all_f[-50:]  # last 50

    def resolve_market(self, winner: str) -> dict:
        """Market kapandiginda binary resolution hesabi.

        winner: "UP" veya "DOWN"
        Kazanan shares × $1, kaybeden shares × $0.
        """
        pm = self.polymarket_pnl()
        total_cost = pm["total_cost"]
        total_rev = pm["total_revenue"]

        if winner == "UP":
            payout = self.up.shares * 1.0
        else:
            payout = self.down.shares * 1.0

        resolved_pnl = (payout + total_rev) - total_cost

        return {
            "winner": winner,
            "payout": round(payout, 4),
            "total_cost": round(total_cost, 4),
            "total_revenue": round(total_rev, 4),
            "resolved_pnl": round(resolved_pnl, 4),
        }

    def snapshot_for_history(self, question: str, winner: str,
                             btc_close: float = 0, ptb: float = 0) -> dict:
        """Market kapandiginda gecmis tablosu icin snapshot.

        winner: "UP" veya "DOWN" — BTC > PTB ise UP, degilse DOWN
        Binary resolution: kazanan shares × $1, kaybeden = $0
        """
        res = self.resolve_market(winner)
        return {
            "ts": time.time(),
            "question": question,
            "winner": winner,
            "btc_close": round(btc_close, 2),
            "ptb": round(ptb, 2),
            "up_shares": self.up.shares,
            "up_cost": round(self.up.total_cost, 2),
            "up_revenue": round(self.up.total_revenue, 2),
            "down_shares": self.down.shares,
            "down_cost": round(self.down.total_cost, 2),
            "down_revenue": round(self.down.total_revenue, 2),
            "payout": res["payout"],
            "total_cost": res["total_cost"],
            "resolved_pnl": res["resolved_pnl"],
            "fills": self.up.num_buys + self.up.num_sells + self.down.num_buys + self.down.num_sells,
        }
