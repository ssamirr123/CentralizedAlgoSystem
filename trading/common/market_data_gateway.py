"""
MarketDataGateway -- Phase 16.6: the thin, structurally read-only seam
connecting the existing trading/market_data/ provider layer to
trading/common/strategy_runtime.py, so a strategy CAN receive normalized
market data before generating an OrderIntent -- broker-agnostic, and
never itself a path to order execution.

    MarketDataProvider (trading/market_data/providers/*.py -- UNCHANGED)
              |
              v
    MarketDataSource (protocol, THIS MODULE -- deliberately smaller)
              |
              v
    gather_market_data()  -- fail-closed on missing/stale/invalid/partial
              |
              v
    StrategyRuntime.run_once()  (Phase 16.5, extended)
              |
              v
    Strategy.generate_order_intents(market_data=...)

This module never imports a broker adapter, never imports an execution
engine, never imports anything under the human-authorized
live-authorization/canary modules, and never calls anything but a
MarketDataProvider's own read-only quote methods (get_index_quote /
get_option_quote) -- trading/market_data/providers/base.py's own
MarketDataProvider ABC already structurally forbids any order-management
method from existing on that interface at all, so there is nothing
mutating here to accidentally call.

FAIL-CLOSED, by design: gather_market_data() returns a usable snapshot
ONLY when every required instrument's quote is present, valid (has a
positive last-traded-price), and fresh (age <= max_age_seconds). Missing,
stale, invalid, or PARTIAL data (e.g. spot present but not CE) all result
in None -- the caller (StrategyRuntime) then skips generate_order_intents()
entirely for that cycle. Nothing here ever fabricates a quote or silently
substitutes a stale one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from trading.market_data.schemas import IndexQuote, OptionQuote

__all__ = [
    "DEFAULT_MAX_DATA_AGE_SECONDS", "MarketDataStatus", "MarketDataCheckResult",
    "MarketDataSource", "FixedMarketDataSource", "ProviderMarketDataSource",
    "check_market_data", "gather_market_data",
]

# Phase 16.6: the freshness threshold this gateway enforces when a caller
# does not specify one. Deliberately conservative for a strategy-
# evaluation consumer (shorter than a UI display's own staleness
# tolerance in trading/market_data/cache.py) -- documented here, not
# silently hardcoded elsewhere, and always overridable per call.
DEFAULT_MAX_DATA_AGE_SECONDS = 60.0


class MarketDataStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    NO_DATA = "NO_DATA"
    STALE = "STALE"
    INVALID = "INVALID"
    PROVIDER_ERROR = "PROVIDER_ERROR"


@dataclass(frozen=True)
class MarketDataCheckResult:
    instrument: str
    status: MarketDataStatus
    quote: IndexQuote | OptionQuote | None
    age_seconds: float | None
    reason: str


class MarketDataSource(Protocol):
    """The ONLY capability the strategy runtime needs from a market-data
    layer -- deliberately much smaller than the full MarketDataProvider
    interface (no connect/subscribe/historical-candle method here), so a
    test or a simple adapter can implement it in a few lines."""

    def get_quote(self, instrument: str) -> IndexQuote | OptionQuote | None:
        ...


class FixedMarketDataSource:
    """A trivial, in-memory MarketDataSource for tests and fixture/replay
    use. Never calls a network, never imports a broker SDK. Not a second
    market-data abstraction -- it implements the exact one-method
    MarketDataSource protocol above, nothing more."""

    def __init__(self, quotes: dict[str, IndexQuote | OptionQuote] | None = None) -> None:
        self._quotes = dict(quotes or {})

    def set_quote(self, instrument: str, quote: IndexQuote | OptionQuote | None) -> None:
        if quote is None:
            self._quotes.pop(instrument, None)
        else:
            self._quotes[instrument] = quote

    def get_quote(self, instrument: str) -> IndexQuote | OptionQuote | None:
        return self._quotes.get(instrument)


class ProviderMarketDataSource:
    """Adapts an existing trading.market_data.providers.base.MarketDataProvider
    (e.g. ICICIBreezeProvider) to the MarketDataSource protocol above.
    Read-only: calls only get_index_quote() -- the provider interface has
    no mutating method to call by mistake in the first place. Any
    exception is caught by check_market_data() below and reported as
    PROVIDER_ERROR, never raised into the strategy runtime.

    Known, documented limitation: only index quotes are wired this phase
    (get_option_quote() is not called here) -- no registered strategy
    requires an option quote yet (Section 3 of this phase's report), and
    adding it is a small, natural future extension once one does."""

    def __init__(self, provider: object) -> None:
        self._provider = provider

    def get_quote(self, instrument: str) -> IndexQuote | OptionQuote | None:
        return self._provider.get_index_quote(instrument)  # type: ignore[attr-defined]


def check_market_data(
    source: MarketDataSource, instrument: str, *, max_age_seconds: float = DEFAULT_MAX_DATA_AGE_SECONDS,
) -> MarketDataCheckResult:
    try:
        quote = source.get_quote(instrument)
    except Exception as exc:  # noqa: BLE001 -- fail closed, never raise into the runtime
        return MarketDataCheckResult(
            instrument=instrument, status=MarketDataStatus.PROVIDER_ERROR, quote=None,
            age_seconds=None, reason=f"PROVIDER_ERROR: {exc}",
        )

    if quote is None:
        return MarketDataCheckResult(
            instrument=instrument, status=MarketDataStatus.NO_DATA, quote=None, age_seconds=None,
            reason=f"MARKET_DATA_UNAVAILABLE: no quote for {instrument!r}",
        )

    ltp = getattr(quote, "ltp", None)
    if ltp is None or ltp <= 0:
        return MarketDataCheckResult(
            instrument=instrument, status=MarketDataStatus.INVALID, quote=quote, age_seconds=None,
            reason="INVALID_MARKET_DATA: quote has no valid last-traded price",
        )

    timestamp = quote.provider_timestamp or quote.received_at
    if timestamp is None:
        return MarketDataCheckResult(
            instrument=instrument, status=MarketDataStatus.INVALID, quote=quote, age_seconds=None,
            reason="INVALID_MARKET_DATA: quote has no timestamp",
        )
    ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    if age_seconds > max_age_seconds:
        return MarketDataCheckResult(
            instrument=instrument, status=MarketDataStatus.STALE, quote=quote, age_seconds=age_seconds,
            reason=f"MARKET_DATA_STALE: quote is {age_seconds:.1f}s old (max {max_age_seconds:.1f}s)",
        )

    return MarketDataCheckResult(
        instrument=instrument, status=MarketDataStatus.AVAILABLE, quote=quote, age_seconds=age_seconds, reason="",
    )


def gather_market_data(
    source: MarketDataSource | None, instruments: tuple[str, ...], *,
    max_age_seconds: float = DEFAULT_MAX_DATA_AGE_SECONDS,
) -> tuple[dict[str, IndexQuote | OptionQuote] | None, list[MarketDataCheckResult]]:
    """Returns (snapshot_or_None, per-instrument results). The snapshot is
    None unless EVERY requested instrument resolved AVAILABLE -- fail-
    closed on partial data (e.g. spot present but CE missing means no
    execution, never a partial snapshot -- see this phase's own explicit
    rule)."""
    if not instruments:
        return None, []
    if source is None:
        return None, [
            MarketDataCheckResult(
                instrument=i, status=MarketDataStatus.NO_DATA, quote=None, age_seconds=None,
                reason="MARKET_DATA_UNAVAILABLE: no market data source is configured",
            )
            for i in instruments
        ]
    results = [check_market_data(source, i, max_age_seconds=max_age_seconds) for i in instruments]
    if all(r.status == MarketDataStatus.AVAILABLE for r in results):
        return {r.instrument: r.quote for r in results}, results
    return None, results
