"""
Normalized market-data domain types.

These are the *internal* shapes every provider must return -- plain frozen
dataclasses, deliberately not Pydantic (the HTTP response models come
later, in the Phase 11 API layer, and will be built from these).

Field rule: if a provider does not supply a value, it is ``None``. Never
fabricate a number (Phase "OPTION DATA"). ``change`` / ``change_percent``
are *derived* from ltp and prev_close only when both are present -- that
is arithmetic on real values, not invention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _derive_change(ltp: float | None, prev_close: float | None) -> tuple[float | None, float | None]:
    if ltp is None or prev_close is None or prev_close == 0:
        return None, None
    change = round(ltp - prev_close, 4)
    return change, round((change / prev_close) * 100, 4)


@dataclass(frozen=True)
class IndexQuote:
    symbol: str  # internal symbol, e.g. "NIFTY"
    ltp: float | None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    volume: int | None = None
    provider_timestamp: datetime | None = None  # exchange/provider time of the quote
    received_at: datetime = field(default_factory=_now_utc)  # local receive time
    provider: str = ""

    @classmethod
    def build(
        cls,
        symbol: str,
        *,
        ltp: float | None,
        open: float | None = None,
        high: float | None = None,
        low: float | None = None,
        prev_close: float | None = None,
        volume: int | None = None,
        provider_timestamp: datetime | None = None,
        provider: str = "",
    ) -> "IndexQuote":
        change, change_pct = _derive_change(ltp, prev_close)
        return cls(
            symbol=symbol, ltp=ltp, open=open, high=high, low=low, prev_close=prev_close,
            change=change, change_percent=change_pct, volume=volume,
            provider_timestamp=provider_timestamp, provider=provider,
        )


@dataclass(frozen=True)
class OptionQuote:
    symbol: str  # internal option symbol
    underlying: str
    expiry: date
    strike: float
    option_type: str  # "CE" | "PE"
    ltp: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    volume: int | None = None
    oi: int | None = None
    oi_change: int | None = None
    bid: float | None = None
    ask: float | None = None
    iv: float | None = None
    vwap: float | None = None
    provider_timestamp: datetime | None = None
    received_at: datetime = field(default_factory=_now_utc)
    provider: str = ""

    @classmethod
    def build(
        cls,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        ltp: float | None = None,
        open: float | None = None,
        high: float | None = None,
        low: float | None = None,
        prev_close: float | None = None,
        volume: int | None = None,
        oi: int | None = None,
        oi_change: int | None = None,
        bid: float | None = None,
        ask: float | None = None,
        iv: float | None = None,
        vwap: float | None = None,
        provider_timestamp: datetime | None = None,
        provider: str = "",
    ) -> "OptionQuote":
        from trading.market_data.symbols import make_option_symbol

        change, change_pct = _derive_change(ltp, prev_close)
        return cls(
            symbol=make_option_symbol(underlying, expiry, strike, option_type),
            underlying=underlying.upper(), expiry=expiry, strike=float(strike),
            option_type=option_type.upper(),
            ltp=ltp, open=open, high=high, low=low, prev_close=prev_close,
            change=change, change_percent=change_pct, volume=volume, oi=oi, oi_change=oi_change,
            bid=bid, ask=ask, iv=iv, vwap=vwap,
            provider_timestamp=provider_timestamp, provider=provider,
        )


@dataclass(frozen=True)
class OptionChainRow:
    strike: float
    call: OptionQuote | None = None
    put: OptionQuote | None = None


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    expiry: date
    spot: float | None
    atm_strike: float | None
    generated_at: datetime
    rows: list[OptionChainRow]
    provider: str = ""


@dataclass(frozen=True)
class Candle:
    symbol: str  # internal symbol
    interval: str  # e.g. "1minute", "5minute", "1day"
    timestamp: datetime  # candle START, timezone-aware
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    oi: int | None = None
