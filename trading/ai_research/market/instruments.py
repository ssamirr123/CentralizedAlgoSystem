"""
Canonical instrument model (Section 10) and the initial Indian instrument
universe (Section 11/27).

Design choice on NIFTY 50 constituents (Section 27's own "prefer a
maintainable instrument source... do not manually duplicate large static
lists"): rather than hardcode and maintain a 50-name list that drifts out
of sync with NSE's periodic index reconstitution (typically a few changes
per year), equities are resolved GENERICALLY -- any syntactically valid
NSE symbol resolves to a canonical Instrument via `resolve_equity()`,
exactly like the existing US/generic path already treats any ticker
string. A small, explicitly-labeled SEED list of well-known large-cap
names is kept only for instrument-search discoverability (Section 26) --
it is documentation/UX convenience, never treated as an authoritative or
complete constituent list, and resolution never depends on a symbol
being in it.

Canonical IDs (Section 11: "do not make display names the primary key")
are short, stable, uppercase strings -- NIFTY_50, NIFTY_BANK,
NIFTY_FIN_SERVICE -- distinct from both the NSE index label ("NIFTY 50")
and any provider's own symbol ("^NSEI").
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Market(str, Enum):
    US = "US"
    INDIA = "INDIA"


class Exchange(str, Enum):
    """Section 5: NSE only for now; structured so BSE (or a US exchange
    enum) could be added later without reshaping callers."""

    NSE = "NSE"
    GENERIC = "GENERIC"  # the existing US/global path (no specific exchange modeled)


class InstrumentType(str, Enum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"


@dataclass(frozen=True)
class Instrument:
    """Normalized instrument metadata (Section 10). `canonical_id` is the
    stable primary key; `symbol` is the human-entered/display ticker."""

    canonical_id: str
    market: Market
    exchange: Exchange
    instrument_type: InstrumentType
    symbol: str
    display_name: str
    currency: str
    timezone: str


# --------------------------------------------------------------------------- #
# Section 11 -- Indian indices. Canonical names/index labels verified live
# against NSE's own index API (https://www.nseindia.com/api/allIndices,
# fetched 2026-09-22): "NIFTY 50", "NIFTY BANK", and "NIFTY FINANCIAL
# SERVICES" (indexSymbol "NIFTY FIN SERVICE") are the real NSE index
# names. "FINNIFTY" is the popular name for the derivatives contract on
# the NIFTY FINANCIAL SERVICES index, not a distinct NSE index itself --
# recorded as an alias, not a second instrument.
# --------------------------------------------------------------------------- #
NIFTY_50 = Instrument(
    canonical_id="NIFTY_50", market=Market.INDIA, exchange=Exchange.NSE,
    instrument_type=InstrumentType.INDEX, symbol="NIFTY 50", display_name="NIFTY 50",
    currency="INR", timezone="Asia/Kolkata",
)
NIFTY_BANK = Instrument(
    canonical_id="NIFTY_BANK", market=Market.INDIA, exchange=Exchange.NSE,
    instrument_type=InstrumentType.INDEX, symbol="NIFTY BANK", display_name="NIFTY Bank (Bank Nifty)",
    currency="INR", timezone="Asia/Kolkata",
)
NIFTY_FIN_SERVICE = Instrument(
    canonical_id="NIFTY_FIN_SERVICE", market=Market.INDIA, exchange=Exchange.NSE,
    instrument_type=InstrumentType.INDEX, symbol="NIFTY FIN SERVICE",
    display_name="NIFTY Financial Services (FinNifty)",
    currency="INR", timezone="Asia/Kolkata",
)

_INDEX_ALIASES: dict[str, Instrument] = {
    "NIFTY": NIFTY_50, "NIFTY50": NIFTY_50, "NIFTY 50": NIFTY_50, "NIFTY_50": NIFTY_50,
    "BANKNIFTY": NIFTY_BANK, "NIFTY BANK": NIFTY_BANK, "NIFTY_BANK": NIFTY_BANK, "BANK NIFTY": NIFTY_BANK,
    "FINNIFTY": NIFTY_FIN_SERVICE, "NIFTY FIN SERVICE": NIFTY_FIN_SERVICE,
    "NIFTY FINANCIAL SERVICES": NIFTY_FIN_SERVICE, "NIFTY_FIN_SERVICE": NIFTY_FIN_SERVICE,
}

INDIA_INDICES: tuple[Instrument, ...] = (NIFTY_50, NIFTY_BANK, NIFTY_FIN_SERVICE)

# Section 26 discoverability seed ONLY -- see module docstring. Not
# authoritative, not complete, not required for resolution to work.
_EQUITY_SEARCH_SEED: dict[str, str] = {
    "RELIANCE": "Reliance Industries",
    "TCS": "Tata Consultancy Services",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "INFY": "Infosys",
    "HINDUNILVR": "Hindustan Unilever",
    "ITC": "ITC",
    "SBIN": "State Bank of India",
    "BHARTIARTL": "Bharti Airtel",
    "KOTAKBANK": "Kotak Mahindra Bank",
    "LT": "Larsen & Toubro",
    "AXISBANK": "Axis Bank",
    "ASIANPAINT": "Asian Paints",
    "MARUTI": "Maruti Suzuki India",
    "TITAN": "Titan Company",
}

_NSE_SYMBOL_PATTERN_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&-")


class InvalidSymbolError(ValueError):
    pass


class InvalidMarketError(ValueError):
    pass


def resolve_index(query: str) -> Instrument | None:
    """Resolve a user-entered index name/alias to a canonical Instrument,
    or None if it doesn't match a known Indian index."""
    return _INDEX_ALIASES.get(query.strip().upper())


def resolve_equity(symbol: str) -> Instrument:
    """Section 27: generic resolution -- any syntactically valid NSE
    equity symbol resolves, not just a hardcoded constituent list."""
    sym = symbol.strip().upper()
    if not sym or len(sym) > 32 or not set(sym) <= _NSE_SYMBOL_PATTERN_CHARS:
        raise InvalidSymbolError(f"{symbol!r} is not a valid NSE equity symbol")
    display = _EQUITY_SEARCH_SEED.get(sym, sym)
    return Instrument(
        canonical_id=f"NSE_EQ_{sym}", market=Market.INDIA, exchange=Exchange.NSE,
        instrument_type=InstrumentType.EQUITY, symbol=sym, display_name=display,
        currency="INR", timezone="Asia/Kolkata",
    )


def resolve_instrument(market: Market | str, symbol: str) -> Instrument:
    """The one entry point callers should use: resolves an index alias
    first, falling back to generic equity resolution, for market=INDIA.
    market=US returns a minimal generic Instrument (Section 30: existing
    US/generic research must be unaffected)."""
    if isinstance(market, Market):
        m = market
    else:
        try:
            m = Market(market)
        except ValueError as exc:
            raise InvalidMarketError(f"unsupported market {market!r}") from exc
    if m == Market.US:
        sym = symbol.strip().upper()
        return Instrument(
            canonical_id=f"GENERIC_{sym}", market=Market.US, exchange=Exchange.GENERIC,
            instrument_type=InstrumentType.EQUITY, symbol=sym, display_name=sym,
            currency="USD", timezone="America/New_York",
        )
    if m == Market.INDIA:
        idx = resolve_index(symbol)
        if idx is not None:
            return idx
        return resolve_equity(symbol)
    raise InvalidMarketError(f"unsupported market {market!r}")


def search_instruments(market: Market | str, query: str, limit: int = 20) -> list[Instrument]:
    """Section 26: backend-supported search -- the frontend never
    maintains its own hardcoded stock list. Matches indices by alias and
    the seed list by symbol/display-name substring; a syntactically valid
    but unseen symbol is still resolvable directly via resolve_equity(),
    it simply won't appear in a partial-text search result."""
    m = Market(market) if not isinstance(market, Market) else market
    if m != Market.INDIA:
        return []
    q = query.strip().upper()
    if not q:
        return []
    results: list[Instrument] = []
    for alias, inst in _INDEX_ALIASES.items():
        if q in alias and inst not in results:
            results.append(inst)
    for sym, name in _EQUITY_SEARCH_SEED.items():
        if q in sym or q in name.upper():
            results.append(resolve_equity(sym))
    # Also offer the literal query as a directly-resolvable equity when it
    # looks syntactically valid, so a symbol outside the seed list is
    # still discoverable by typing it in full.
    try:
        direct = resolve_equity(q)
        if all(r.canonical_id != direct.canonical_id for r in results):
            results.append(direct)
    except InvalidSymbolError:
        pass
    return results[:limit]
