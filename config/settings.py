"""Bot configuration and settings."""

__version__ = "2.1.0"

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class PolymarketConfig:
    """Polymarket API configuration."""
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    wallet_address: str = os.getenv("POLY_WALLET_ADDRESS", "")
    api_key: str = os.getenv("POLY_API_KEY", "")
    api_secret: str = os.getenv("POLY_API_SECRET", "")
    api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")

    clob_url: str = "https://clob.polymarket.com"
    ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    chain_id: int = 137  # Polygon


@dataclass
class StrategyFlags:
    """Toggleable strategy features."""
    maker_zero_fee: bool = True          # Assume zero maker fee (tighter spreads)
    inventory_skew: bool = True          # Skew quotes based on inventory
    dual_quote: bool = True              # Quote both bid and ask
    auto_hedge: bool = False             # Auto-hedge via opposite token
    use_btc_signal: bool = True          # Use real BTC price for directional quoting
    shares_gap_max_pct: float = 1.3      # UP/DOWN shares arasi max % fark
    rebalance_enabled: bool = True       # Shares dengeleme aktif
    contrarian_enabled: bool = True      # Ucuz token biriktirme aktif
    contrarian_budget_pct: float = 0.15  # Contrarian butce orani


@dataclass
class QuotingConfig:
    """Market making / quoting parameters."""
    # Spread around mid-price (each side)
    base_spread_bps: float = 200.0       # 2% base half-spread
    min_spread_bps: float = 100.0        # 1% minimum half-spread

    # Order size
    order_size: float = 10.0             # USDC per side
    min_order_size: float = 5.0          # Polymarket minimum

    # Inventory skew: how aggressively to skew quotes per unit of inventory
    inventory_skew_bps: float = 50.0     # bps per contract of net inventory

    # Quote lifetime: GTD expiration offset in seconds
    # Must be > 60s due to Polymarket's 60-second security threshold
    quote_lifetime_sec: int = 90         # 90 seconds GTD expiration
    quote_refresh_sec: float = 15.0      # Refresh quotes every 15 seconds

    # Tick size (will be updated from market data)
    tick_size: float = 0.01


@dataclass
class RiskConfig:
    """Risk management parameters."""
    max_position: float = 100.0          # Max net position (contracts)
    max_loss_usdc: float = 50.0          # Max unrealized loss before halting
    max_open_orders: int = 10            # Max simultaneous open orders
    heartbeat_interval_sec: float = 5.0  # Send heartbeat every 5 seconds


@dataclass
class BotConfig:
    """Top-level bot configuration."""
    test_mode: bool = os.getenv("TEST_MODE", "true").lower() == "true"
    dashboard_host: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    dashboard_port: int = int(os.getenv("DASHBOARD_PORT", "18998"))
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    quoting: QuotingConfig = field(default_factory=QuotingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyFlags = field(default_factory=StrategyFlags)

    # BTC 5-minute market
    market_slug_prefix: str = "btc-updown-5m"
