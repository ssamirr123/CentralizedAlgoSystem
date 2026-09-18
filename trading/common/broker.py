"""
Broker abstraction. Every broker adapter (paper, Zerodha, AngelOne, ICICI
Breeze, ...) implements BrokerClient so strategy code never talks to a
broker SDK directly — it only depends on this interface. Swapping brokers
means swapping the adapter, not rewriting the strategy.

Safety: place_order/cancel_order on every REAL adapter must check
config.is_live and refuse to submit unless TRADING_MODE=live. This is
enforced per-adapter (see brokers/*.py) rather than centrally, so each
adapter is explicit about the check rather than relying on a base class
default that could be silently bypassed by an override.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from trading.common.config import TradingConfig


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass(frozen=True)
class Quote:
    symbol: str
    last_price: float
    timestamp: str


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    status: str  # e.g. "PLACED", "REJECTED", "FILLED"
    message: str = ""


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float


class BrokerConnectionError(RuntimeError):
    """Raised for connectivity-type failures (network, timeout, broker-side
    outage) that are worth retrying with backoff."""


class BrokerConfigError(RuntimeError):
    """Raised for permanent misconfiguration (missing/invalid credentials).
    Deliberately NOT a subclass of BrokerConnectionError — retrying a
    missing API key with exponential backoff just delays an inevitable
    failure. main.py's connect_with_retry fails fast on this instead."""


class LiveTradingDisabledError(RuntimeError):
    """Raised when a real order is attempted while TRADING_MODE != 'live'."""


class BrokerAuthenticationError(RuntimeError):
    """Raised when a broker rejects a login/session attempt at runtime
    (bad password, expired/invalid TOTP, revoked session, ...). Distinct
    from BrokerConfigError, which is for structurally missing credentials
    before any call is even attempted -- this is for a call the broker
    itself refused. Not automatically retryable: an adapter may classify a
    specific case as retryable, but the generic default is not to retry an
    authentication failure the same way as a connectivity blip."""


class ReadOnlyModeError(RuntimeError):
    """Raised when a broker adapter constructed/configured in read-only
    mode (see e.g. AngelOneBroker's ANGEL_READ_ONLY support) refuses a
    mutating call (place_order/modify_order/cancel_order).

    Deliberately independent of LiveTradingDisabledError/TRADING_MODE:
    read-only mode is a separate, higher-priority safety gate intended for
    manual read-only diagnostic tooling that must never be able to submit
    a real order, regardless of what TRADING_MODE happens to be set to."""


class BrokerRateLimitError(BrokerConnectionError):
    """Raised when a broker rejects a call for exceeding its own rate
    limit. Subclasses BrokerConnectionError (rate limits are a connectivity-
    type, retryable failure) so callers that already treat
    BrokerConnectionError as retryable get sane behavior for free, while
    still being able to special-case rate limits (e.g. a longer backoff)
    where useful."""


class BrokerClient(ABC):
    """Common interface every broker adapter must implement."""

    # Phase 13 observability: True only for adapters that NEVER reach a
    # real broker order API (ShadowBroker, ConnectedShadowBroker) --
    # StrategyExecutionEngine reads this to classify a fill as
    # record_simulated_fill() vs record_real_fill() without needing to
    # import those specific classes (avoiding a layering dependency from
    # the generic execution engine onto specific broker adapters). Every
    # real adapter (Angel/Dhan/ICICI/Zerodha/Paper) inherits the default.
    is_simulated: bool = False

    @abstractmethod
    def connect(self) -> None:
        """Establish the broker session. Raise BrokerConnectionError on failure."""

    @abstractmethod
    def disconnect(self) -> None:
        """Cleanly close the broker session."""

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        ...

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
    ) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        ...


def create_broker(config: TradingConfig) -> BrokerClient:
    """Factory: instantiate the configured broker adapter."""
    name = config.broker_name

    if name == "paper":
        from trading.common.brokers.paper_broker import PaperBroker

        return PaperBroker()

    if name == "zerodha":
        from trading.common.brokers.zerodha_kite import ZerodhaKiteBroker

        return ZerodhaKiteBroker(config)

    if name == "angelone":
        from trading.common.brokers.angelone import AngelOneBroker

        return AngelOneBroker(config)

    if name == "icici_breeze":
        from trading.common.brokers.icici_breeze import ICICIBreezeBroker

        return ICICIBreezeBroker(config)

    if name == "dhan":
        from trading.common.brokers.dhan import DhanBroker

        return DhanBroker(config)

    raise ValueError(
        f"Unknown BROKER '{name}'. Expected one of: paper, zerodha, angelone, icici_breeze, dhan."
    )
