"""Result/status shapes for the historical market-data bridge."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from trading.market_data.schemas import Candle


class DataStatus:
    """Section 30 diagnostics -- explains why technical data may be
    unavailable, without exposing provider internals."""

    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    STALE = "STALE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    UNSUPPORTED = "UNSUPPORTED"  # e.g. market=US, no Breeze coverage


class DataSource:
    DB = "TIMESCALEDB"
    PROVIDER = "ICICI_BREEZE"
    MIXED = "MIXED"
    NONE = "NONE"


@dataclass(frozen=True)
class HistoricalDataResult:
    """Section 19 provenance + Section 30 diagnostics, in one shape."""

    status: str  # DataStatus.*
    source: str  # DataSource.*
    provider: str  # "icici_breeze"
    canonical_id: str
    symbol: str  # Stage-19 internal_symbol actually queried
    interval: str
    requested_from: date
    requested_to: date
    cutoff_applied: datetime | None  # Section 21/22 -- None if no cutoff was needed
    candles: tuple[Candle, ...] = field(default_factory=tuple)
    db_hit_count: int = 0
    provider_call_count: int = 0
    candles_fetched: int = 0
    candles_stored: int = 0
    missing_days: tuple[date, ...] = field(default_factory=tuple)
    detail: str | None = None  # human-readable reason, e.g. why PARTIAL/MISSING
