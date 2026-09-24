"""Canonical instrument resolution tests."""
from __future__ import annotations

import pytest

from trading.ai_research.market.instruments import (
    Exchange,
    InstrumentType,
    InvalidMarketError,
    InvalidSymbolError,
    Market,
    resolve_equity,
    resolve_index,
    resolve_instrument,
    search_instruments,
)


@pytest.mark.parametrize("query", ["NIFTY", "NIFTY50", "NIFTY 50", "nifty 50"])
def test_nifty_50_resolution(query):
    inst = resolve_index(query)
    assert inst is not None
    assert inst.canonical_id == "NIFTY_50"
    assert inst.instrument_type == InstrumentType.INDEX
    assert inst.currency == "INR"
    assert inst.exchange == Exchange.NSE


@pytest.mark.parametrize("query", ["BANKNIFTY", "NIFTY BANK", "bank nifty"])
def test_banknifty_resolution(query):
    inst = resolve_index(query)
    assert inst is not None
    assert inst.canonical_id == "NIFTY_BANK"


@pytest.mark.parametrize("query", ["FINNIFTY", "NIFTY FIN SERVICE"])
def test_finnifty_resolution(query):
    inst = resolve_index(query)
    assert inst is not None
    assert inst.canonical_id == "NIFTY_FIN_SERVICE"


def test_unknown_index_alias_returns_none():
    assert resolve_index("SENSEX") is None


def test_equity_resolution_generic_not_requiring_seed_list():
    """Section 27: any syntactically valid symbol resolves, not just a
    hardcoded constituent list."""
    inst = resolve_equity("ZOMATO")  # not in the seed list
    assert inst.instrument_type == InstrumentType.EQUITY
    assert inst.symbol == "ZOMATO"
    assert inst.currency == "INR"
    assert inst.canonical_id == "NSE_EQ_ZOMATO"


def test_equity_resolution_uses_seed_display_name_when_known():
    inst = resolve_equity("reliance")
    assert inst.symbol == "RELIANCE"
    assert inst.display_name == "Reliance Industries"


def test_equity_symbol_with_ampersand():
    inst = resolve_equity("M&M")
    assert inst.symbol == "M&M"


def test_invalid_equity_symbol_rejected():
    with pytest.raises(InvalidSymbolError):
        resolve_equity("bad symbol!")
    with pytest.raises(InvalidSymbolError):
        resolve_equity("")


def test_resolve_instrument_india_prefers_index_over_equity():
    inst = resolve_instrument(Market.INDIA, "NIFTY")
    assert inst.instrument_type == InstrumentType.INDEX


def test_resolve_instrument_india_equity_fallback():
    inst = resolve_instrument(Market.INDIA, "TCS")
    assert inst.instrument_type == InstrumentType.EQUITY
    assert inst.currency == "INR"


def test_resolve_instrument_us_generic_unaffected():
    """Section 30: existing US/generic research must be unaffected."""
    inst = resolve_instrument(Market.US, "AAPL")
    assert inst.market == Market.US
    assert inst.exchange == Exchange.GENERIC
    assert inst.currency == "USD"
    assert inst.symbol == "AAPL"


def test_resolve_instrument_invalid_market():
    with pytest.raises(InvalidMarketError):
        resolve_instrument("MARS", "AAPL")


def test_search_instruments_india_finds_index_and_seed_equity():
    results = search_instruments(Market.INDIA, "nifty")
    canonical_ids = {r.canonical_id for r in results}
    assert "NIFTY_50" in canonical_ids
    assert "NIFTY_BANK" in canonical_ids


def test_search_instruments_finds_symbol_outside_seed_list():
    results = search_instruments(Market.INDIA, "ZOMATO")
    assert any(r.symbol == "ZOMATO" for r in results)


def test_search_instruments_us_returns_empty():
    assert search_instruments(Market.US, "AAPL") == []


def test_search_instruments_empty_query_returns_empty():
    assert search_instruments(Market.INDIA, "") == []
