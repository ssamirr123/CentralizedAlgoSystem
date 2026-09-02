"""
Provider interface for the market-data engine.

Rule 19: this abstraction exists so Zerodha / others can be added later
without touching the layers above. Rule (Phase 2): a market-data provider
must NEVER expose ``place_order`` / ``modify_order`` / ``cancel_order`` --
order execution lives in ``trading/common/broker.py`` and stays separate.

Error taxonomy (mirrors ``trading/common/broker.py``'s split):

* ``ProviderAuthError``       -- missing / expired / rejected session. A
  permanent condition until the operator provisions a new session; the
  caller must NOT retry with backoff.
* ``ProviderConnectionError`` -- network / timeout / provider outage.
  Retryable with bounded exponential backoff (Phase 16).
* ``ProviderRateLimitError``  -- provider throttled us. Back off longer.
* ``ProviderDataError``       -- the response arrived but is malformed or
  missing something required.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import date, datetime

from trading.market_data.schemas import Candle, IndexQuote, OptionChain, OptionQuote
from trading.market_data.symbols import Instrument


class ProviderError(RuntimeError):
    """Base class for all market-data provider failures."""


class ProviderAuthError(ProviderError):
    """Missing / expired / rejected credentials or session. Not retryable."""


class ProviderConnectionError(ProviderError):
    """Network / timeout / provider outage. Retryable with backoff."""


class ProviderRateLimitError(ProviderConnectionError):
    """Provider throttled the client. Retryable, but back off harder."""


class ProviderDataError(ProviderError):
    """A response was received but is malformed / missing required fields."""


# A streaming tick is delivered as a normalized quote. Callbacks may run
# on a provider-owned background thread -- keep them fast and non-blocking
# (push to a queue / cache; do not do I/O in the callback).
TickCallback = Callable[["IndexQuote | OptionQuote"], None]


class MarketDataProvider(ABC):
    """Read-only market-data source. No order-management methods, ever."""

    #: stable identifier, e.g. "icici_breeze"
    name: str = "base"

    # --- lifecycle ------------------------------------------------------
    @abstractmethod
    def connect(self) -> None:
        """Establish the provider session. Raise ``ProviderAuthError`` for a
        credential/session problem, ``ProviderConnectionError`` for a
        transient one."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the session / websocket. Idempotent."""

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    # --- snapshot reads ----------------------------------------------
    @abstractmethod
    def get_index_quote(self, instrument: Instrument) -> IndexQuote:
        ...

    @abstractmethod
    def get_option_quote(self, instrument: Instrument) -> OptionQuote:
        ...

    @abstractmethod
    def get_option_chain(
        self,
        underlying: str,
        expiry: date,
        *,
        right: str | None = None,  # None -> both CE and PE
    ) -> OptionChain:
        ...

    @abstractmethod
    def get_historical_candles(
        self,
        instrument: Instrument,
        interval: str,  # "1minute" | "5minute" | "1day" | ...
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        ...

    # --- instrument master (Phase 5) -------------------------------
    @abstractmethod
    def get_option_instruments(self, underlying: str) -> list[Instrument]:
        """Every currently-listed option contract for ``underlying`` as
        fully-populated ``Instrument``s (expiry / strike / option_type /
        lot_size / tick_size / provider_token). Contracts are resolved
        dynamically from the provider's instrument master -- no token is
        ever hardcoded."""

    # --- streaming --------------------------------------------------
    @abstractmethod
    def subscribe(
        self,
        instruments: Sequence[Instrument],
        on_tick: TickCallback,
        *,
        resolver: "Callable[[dict], Instrument | None] | None" = None,
    ) -> None:
        """Start a realtime feed for ``instruments``; ``on_tick`` is called
        with a normalized quote per update. ``resolver`` maps a raw
        provider tick payload back to an ``Instrument`` (used for option
        ticks, whose payload the provider alone can't always name)."""

    @abstractmethod
    def unsubscribe(self, instruments: Sequence[Instrument]) -> None:
        ...
