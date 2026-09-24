"""
Provider symbol mapping tests -- expected values are the REAL Yahoo
Finance symbols verified live during Phase 7 development (see
symbols.py's module docstring for the exact verification: ^NSEI/
^NSEBANK matched NSE's own live index quotes to the decimal;
NIFTY_FIN_SERVICE.NS matched NSE's "NIFTY FIN SERVICE" quote, while the
superficially-similar ^CNXFIN did NOT -- confirming it is a different
index).
"""
from __future__ import annotations

import pytest

from trading.ai_research.market.instruments import Market, resolve_index, resolve_instrument
from trading.ai_research.market.symbols import (
    UnmappedProviderSymbolError,
    to_breeze_symbol,
    to_tradingagents_symbol,
    to_yahoo_symbol,
)


def test_nifty_50_maps_to_verified_yahoo_symbol():
    inst = resolve_index("NIFTY")
    assert to_yahoo_symbol(inst) == "^NSEI"


def test_banknifty_maps_to_verified_yahoo_symbol():
    inst = resolve_index("BANKNIFTY")
    assert to_yahoo_symbol(inst) == "^NSEBANK"


def test_finnifty_maps_to_verified_yahoo_symbol_not_the_lookalike():
    inst = resolve_index("FINNIFTY")
    # NOT ^CNXFIN -- that resolves to a different NSE index (confirmed by
    # live cross-check against NSE's own quote for each).
    assert to_yahoo_symbol(inst) == "NIFTY_FIN_SERVICE.NS"
    assert to_yahoo_symbol(inst) != "^CNXFIN"


def test_equity_maps_to_dot_ns_convention():
    inst = resolve_instrument(Market.INDIA, "RELIANCE")
    assert to_yahoo_symbol(inst) == "RELIANCE.NS"


def test_equity_with_ampersand_maps_correctly():
    inst = resolve_instrument(Market.INDIA, "M&M")
    assert to_yahoo_symbol(inst) == "M&M.NS"


def test_us_instrument_passes_through_unchanged():
    """Section 30: existing US/generic behavior must be exactly preserved."""
    inst = resolve_instrument(Market.US, "AAPL")
    assert to_yahoo_symbol(inst) == "AAPL"
    assert to_tradingagents_symbol(inst) == "AAPL"


def test_tradingagents_symbol_matches_yahoo_symbol_currently():
    inst = resolve_index("NIFTY")
    assert to_tradingagents_symbol(inst) == to_yahoo_symbol(inst)


# --------------------------------------------------------------------------- #
# Phase 8: to_breeze_symbol() implemented for real (Section 7/8/13/17).
# Returns the Stage-19 provider-agnostic internal_symbol
# (trading.market_data.symbols), NOT a raw Breeze stock_code and NOT a
# Yahoo symbol -- Section 8's own "mapping separation" requirement.
# --------------------------------------------------------------------------- #
def test_nifty_50_maps_to_breeze_stage19_symbol():
    inst = resolve_index("NIFTY")
    assert to_breeze_symbol(inst) == "NIFTY"


def test_banknifty_maps_to_breeze_stage19_symbol():
    inst = resolve_index("BANKNIFTY")
    assert to_breeze_symbol(inst) == "BANKNIFTY"


def test_finnifty_maps_to_breeze_stage19_symbol():
    inst = resolve_index("FINNIFTY")
    assert to_breeze_symbol(inst) == "FINNIFTY"


def test_breeze_mapping_is_separate_from_yahoo_mapping():
    """Section 8: never reuse a Yahoo identifier (e.g. "^NSEI") as the
    Breeze-side canonical identifier."""
    inst = resolve_index("NIFTY")
    assert to_breeze_symbol(inst) != to_yahoo_symbol(inst)


def test_equity_breeze_symbol_is_the_nse_symbol_not_a_breeze_stock_code():
    """to_breeze_symbol() returns the Stage-19 internal_symbol
    ("RELIANCE") -- the deeper translation to Breeze's own stock_code
    ("RELIND") is the provider's own responsibility one layer down (see
    trading/market_data/providers/icici_breeze.py's
    _resolve_equity_stock_code, verified live against ICICI's real
    security master)."""
    inst = resolve_instrument(Market.INDIA, "RELIANCE")
    assert to_breeze_symbol(inst) == "RELIANCE"


def test_us_instrument_has_no_breeze_coverage():
    """Section 31: no silent fallback -- Breeze has no US market-data
    coverage in this system, so this must fail loudly, not guess."""
    inst = resolve_instrument(Market.US, "AAPL")
    with pytest.raises(UnmappedProviderSymbolError):
        to_breeze_symbol(inst)
