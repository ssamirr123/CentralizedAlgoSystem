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


class BrokerClient(ABC):
    """Common interface every broker adapter must implement."""

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

    raise ValueError(
        f"Unknown BROKER '{name}'. Expected one of: paper, zerodha, angelone, icici_breeze."
    )
