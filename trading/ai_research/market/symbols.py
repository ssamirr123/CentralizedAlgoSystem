"""
Provider symbol mapping (Section 12/13/14/17):

    Canonical Instrument
           |
    Provider Symbol Mapper (this module)
           |
    Yahoo / TradingAgents  (Breeze: placeholder only, Section 13 -- "DO
                             NOT implement Breeze API calls")

Yahoo Finance mappings verified LIVE on 2026-09-22 (Section 3 -- not
guessed): `^NSEI` (NIFTY 50) and `^NSEBANK` (NIFTY BANK) returned last
prices of 23329.0 and 56215.55 respectively, matching NSE's own live
`allIndices` API to the last decimal. For FINNIFTY, two candidate Yahoo
symbols were checked -- `^CNXFIN` (27503.6) and `NIFTY_FIN_SERVICE.NS`
(25417.95) -- only the second matches NSE's own "NIFTY FIN SERVICE" quote
(25417.95); `^CNXFIN` is actually Yahoo's symbol for the newer, DIFFERENT
"NIFTY FINANCIAL SERVICES 25/50" capped index, confirmed by cross-checking
against NSE's own allIndices response. Equity mappings (`RELIANCE.NS`,
`TCS.NS`) follow Yahoo's well-documented `<SYMBOL>.NS` convention for
NSE-listed equities, confirmed live for both symbols.
"""
from __future__ import annotations

from trading.ai_research.market.instruments import Exchange, Instrument, InstrumentType, Market

_YAHOO_INDEX_SYMBOLS: dict[str, str] = {
    "NIFTY_50": "^NSEI",
    "NIFTY_BANK": "^NSEBANK",
    "NIFTY_FIN_SERVICE": "NIFTY_FIN_SERVICE.NS",
}


class UnmappedProviderSymbolError(ValueError):
    pass


def to_yahoo_symbol(instrument: Instrument) -> str:
    """The symbol trading_agents_adapter.py's yfinance-backed data
    vendors expect. US/generic instruments pass through unchanged (the
    existing behavior every prior phase already relied on)."""
    if instrument.market == Market.US or instrument.exchange == Exchange.GENERIC:
        return instrument.symbol
    if instrument.exchange != Exchange.NSE:
        raise UnmappedProviderSymbolError(f"no Yahoo mapping for exchange {instrument.exchange!r}")
    if instrument.instrument_type == InstrumentType.INDEX:
        mapped = _YAHOO_INDEX_SYMBOLS.get(instrument.canonical_id)
        if mapped is None:
            raise UnmappedProviderSymbolError(f"no Yahoo mapping for index {instrument.canonical_id!r}")
        return mapped
    # Equity: Yahoo's documented, live-verified <SYMBOL>.NS convention.
    return f"{instrument.symbol}.NS"


def to_tradingagents_symbol(instrument: Instrument) -> str:
    """What gets passed as `instrument`/`company_name` into
    trading_agents_adapter.run_research() -- TradingAgents' own stock-data
    tool ultimately calls the same yfinance vendor, so this is currently
    identical to to_yahoo_symbol(). Kept as a separate named function
    (Section 17: "the adapter should translate... do not make
    TradingAgents responsible for our internal instrument normalization")
    so a future non-yfinance vendor path can diverge without callers
    caring which mapping they asked for."""
    return to_yahoo_symbol(instrument)


_BREEZE_INDEX_INTERNAL_SYMBOLS: dict[str, str] = {
    "NIFTY_50": "NIFTY",
    "NIFTY_BANK": "BANKNIFTY",
    "NIFTY_FIN_SERVICE": "FINNIFTY",
}


def to_breeze_symbol(instrument: Instrument) -> str:
    """Phase 8: returns the Stage-19 provider-agnostic internal_symbol
    (trading.market_data.symbols.Instrument.internal_symbol -- e.g.
    "NIFTY", "RELIANCE") that trading.market_data.providers.icici_breeze
    accepts. Deliberately NOT the raw Breeze stock_code ("NIFTY" vs the
    real Breeze code "NIFTY", or "RELIANCE" vs the real Breeze code
    "RELIND" -- see icici_breeze.py's _resolve_equity_stock_code): that
    translation stays encapsulated one layer further down inside the
    provider (Stage 19's own "rule 20" -- Breeze-specific formats must
    not spread above the provider layer), exactly mirroring how
    to_yahoo_symbol() never leaks a Yahoo-internal quirk either.

    Mapping separation (Section 8): this is a SEPARATE mapping table from
    _YAHOO_INDEX_SYMBOLS -- never reuse "^NSEI" as an internal identifier
    here."""
    if instrument.market == Market.US or instrument.exchange == Exchange.GENERIC:
        raise UnmappedProviderSymbolError("Breeze has no US/generic market-data coverage in this system")
    if instrument.exchange != Exchange.NSE:
        raise UnmappedProviderSymbolError(f"no Breeze mapping for exchange {instrument.exchange!r}")
    if instrument.instrument_type == InstrumentType.INDEX:
        mapped = _BREEZE_INDEX_INTERNAL_SYMBOLS.get(instrument.canonical_id)
        if mapped is None:
            raise UnmappedProviderSymbolError(f"no Breeze mapping for index {instrument.canonical_id!r}")
        return mapped
    return instrument.symbol
