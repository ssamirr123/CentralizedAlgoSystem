"""
Provider-agnostic instrument model.

Rule 20: Breeze-specific (or any single provider's) symbol/token formats
must NOT be spread through the codebase. Everything above the provider
layer refers to an `Instrument` with an `internal_symbol`; each provider
maps that to its own codes internally (see
`providers/icici_breeze.py::_INDEX_CODES`).

Internal symbols
----------------
Indices:  "NIFTY", "BANKNIFTY", "INDIA_VIX", "SENSEX"
Options:  "<UNDERLYING>|<YYYY-MM-DD>|<STRIKE>|<CE|PE>"
          e.g. "NIFTY|2026-09-03|25100|CE"
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class InstrumentType(str, Enum):
    INDEX = "INDEX"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    EQUITY = "EQUITY"


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"  # NSE F&O
    BFO = "BFO"  # BSE F&O


@dataclass(frozen=True)
class Instrument:
    """One tradable/quotable thing, provider-agnostic.

    For an INDEX only `internal_symbol` / `exchange` / `instrument_type`
    are meaningful. For an OPTION the contract fields are populated and
    `internal_symbol` is the canonical string built by
    `make_option_symbol`.
    """

    internal_symbol: str
    exchange: Exchange
    instrument_type: InstrumentType
    # OPTION-only (None for indices) --------------------------------------
    underlying: str | None = None
    expiry: date | None = None
    strike: float | None = None
    option_type: str | None = None  # "CE" | "PE"
    lot_size: int | None = None
    tick_size: float | None = None
    # Provider linkage. Opaque above the provider layer -- only the
    # provider that produced it knows how to interpret ``provider_token``
    # (rule 20). Populated by the instrument master (Phase 5).
    provider: str | None = None
    provider_token: str | None = None

    @property
    def is_option(self) -> bool:
        return self.instrument_type == InstrumentType.OPTION

    @property
    def is_index(self) -> bool:
        return self.instrument_type == InstrumentType.INDEX


# --- Index registry ---------------------------------------------------------
# Provider-agnostic. The four indices Stage 19 must collect. Exchange is a
# fact about the index (SENSEX is BSE); provider codes live in the provider.
INDEX_SYMBOLS: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "INDIA_VIX", "SENSEX")

INDEX_INSTRUMENTS: dict[str, Instrument] = {
    "NIFTY": Instrument("NIFTY", Exchange.NSE, InstrumentType.INDEX),
    "BANKNIFTY": Instrument("BANKNIFTY", Exchange.NSE, InstrumentType.INDEX),
    "INDIA_VIX": Instrument("INDIA_VIX", Exchange.NSE, InstrumentType.INDEX),
    "SENSEX": Instrument("SENSEX", Exchange.BSE, InstrumentType.INDEX),
}

# Accepted aliases -> canonical internal symbol.
_INDEX_ALIASES: dict[str, str] = {
    "NIFTY": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY_50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "NIFTY_BANK": "BANKNIFTY",
    "INDIA_VIX": "INDIA_VIX",
    "INDIAVIX": "INDIA_VIX",
    "VIX": "INDIA_VIX",
    "SENSEX": "SENSEX",
    "BSESENSEX": "SENSEX",
}


def normalize_index_symbol(symbol: str) -> str:
    """Return the canonical internal symbol, or raise KeyError."""
    key = symbol.strip().upper().replace(" ", "_")
    if key in _INDEX_ALIASES:
        return _INDEX_ALIASES[key]
    raise KeyError(f"Unknown index symbol: {symbol!r} (known: {', '.join(INDEX_SYMBOLS)})")


def index_instrument(symbol: str) -> Instrument:
    """Look up one of the four supported index instruments (alias-tolerant)."""
    return INDEX_INSTRUMENTS[normalize_index_symbol(symbol)]


# --- Option symbols -------------------------------------------------------
_OPTION_SEP = "|"


def _fmt_strike(strike: float) -> str:
    # 25100.0 -> "25100"; 25112.5 -> "25112.5"
    return f"{strike:g}"


def make_option_symbol(underlying: str, expiry: date, strike: float, option_type: str) -> str:
    ot = option_type.strip().upper()
    if ot not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")
    return _OPTION_SEP.join(
        [underlying.strip().upper(), expiry.isoformat(), _fmt_strike(float(strike)), ot]
    )


def parse_option_symbol(symbol: str) -> tuple[str, date, float, str]:
    """Inverse of `make_option_symbol`. Returns (underlying, expiry, strike, option_type)."""
    parts = symbol.split(_OPTION_SEP)
    if len(parts) != 4:
        raise ValueError(f"Not an option symbol: {symbol!r}")
    underlying, expiry_s, strike_s, option_type = parts
    try:
        expiry = date.fromisoformat(expiry_s)
        strike = float(strike_s)
    except ValueError as exc:
        raise ValueError(f"Malformed option symbol {symbol!r}: {exc}") from exc
    if option_type not in ("CE", "PE"):
        raise ValueError(f"Malformed option symbol {symbol!r}: bad option_type {option_type!r}")
    return underlying, expiry, strike, option_type


def option_instrument(
    underlying: str,
    expiry: date,
    strike: float,
    option_type: str,
    *,
    exchange: Exchange = Exchange.NFO,
    lot_size: int | None = None,
    tick_size: float | None = None,
    provider: str | None = None,
    provider_token: str | None = None,
) -> Instrument:
    ot = option_type.strip().upper()
    return Instrument(
        internal_symbol=make_option_symbol(underlying, expiry, strike, ot),
        exchange=exchange,
        instrument_type=InstrumentType.OPTION,
        underlying=underlying.strip().upper(),
        expiry=expiry,
        strike=float(strike),
        option_type=ot,
        lot_size=lot_size,
        tick_size=tick_size,
        provider=provider,
        provider_token=provider_token,
    )
