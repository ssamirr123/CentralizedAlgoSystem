"""
Centralized market-data engine (Stage 19 / "Stage 21" in docs to avoid a
clash with the existing realtime Stage 19).

Phase 2 delivers only the *provider layer*: a provider-agnostic interface
(`providers.base.MarketDataProvider`) plus an ICICI Breeze implementation,
and the normalized domain types every provider returns (`schemas`) keyed
by provider-agnostic instruments (`symbols`).

Nothing here places, modifies or cancels orders. Order execution stays in
`trading/common/broker.py`; this package is read-only market data.
"""
from __future__ import annotations

from trading.market_data.schemas import (
    Candle,
    IndexQuote,
    OptionChain,
    OptionChainRow,
    OptionQuote,
)
from trading.market_data.symbols import (
    Exchange,
    Instrument,
    InstrumentType,
    INDEX_INSTRUMENTS,
    INDEX_SYMBOLS,
    index_instrument,
    make_option_symbol,
    option_instrument,
    parse_option_symbol,
)

__all__ = [
    "Candle",
    "IndexQuote",
    "OptionChain",
    "OptionChainRow",
    "OptionQuote",
    "Exchange",
    "Instrument",
    "InstrumentType",
    "INDEX_INSTRUMENTS",
    "INDEX_SYMBOLS",
    "index_instrument",
    "make_option_symbol",
    "option_instrument",
    "parse_option_symbol",
]
