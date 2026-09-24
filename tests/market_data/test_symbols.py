"""Phase 2 -- provider-agnostic instrument model."""
from __future__ import annotations

from datetime import date

import pytest

from trading.market_data.symbols import (
    Exchange,
    InstrumentType,
    INDEX_SYMBOLS,
    equity_instrument,
    index_instrument,
    make_option_symbol,
    normalize_index_symbol,
    option_instrument,
    parse_option_symbol,
)


def test_five_indices_registered():
    """Phase 8 added FINNIFTY (real Breeze code "NIFFIN", verified live
    against ICICI's own security master)."""
    assert INDEX_SYMBOLS == ("NIFTY", "BANKNIFTY", "FINNIFTY", "INDIA_VIX", "SENSEX")


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("nifty", "NIFTY"),
        ("Nifty 50", "NIFTY"),
        ("NIFTYBANK", "BANKNIFTY"),
        ("nifty_bank", "BANKNIFTY"),
        ("vix", "INDIA_VIX"),
        ("IndiaVIX", "INDIA_VIX"),
        ("bsesensex", "SENSEX"),
    ],
)
def test_index_alias_normalization(alias, canonical):
    assert normalize_index_symbol(alias) == canonical


def test_sensex_is_bse_the_others_nse():
    assert index_instrument("SENSEX").exchange is Exchange.BSE
    for s in ("NIFTY", "BANKNIFTY", "INDIA_VIX"):
        assert index_instrument(s).exchange is Exchange.NSE
        assert index_instrument(s).instrument_type is InstrumentType.INDEX
        assert index_instrument(s).is_index and not index_instrument(s).is_option


def test_unknown_index_raises():
    with pytest.raises(KeyError):
        normalize_index_symbol("DOWJONES")


def test_option_symbol_roundtrip():
    sym = make_option_symbol("nifty", date(2026, 9, 3), 25100.0, "ce")
    assert sym == "NIFTY|2026-09-03|25100|CE"
    under, expiry, strike, ot = parse_option_symbol(sym)
    assert (under, expiry, strike, ot) == ("NIFTY", date(2026, 9, 3), 25100.0, "CE")


def test_option_symbol_keeps_fractional_strike():
    sym = make_option_symbol("SENSEX", date(2026, 9, 4), 81250.5, "PE")
    assert sym.endswith("|81250.5|PE")
    assert parse_option_symbol(sym)[2] == 81250.5


def test_make_option_symbol_rejects_bad_right():
    with pytest.raises(ValueError):
        make_option_symbol("NIFTY", date(2026, 9, 3), 25100, "XX")


def test_parse_option_symbol_rejects_non_option():
    with pytest.raises(ValueError):
        parse_option_symbol("NIFTY")


def test_option_instrument_populates_contract_fields():
    inst = option_instrument("NIFTY", date(2026, 9, 3), 25100, "CE", lot_size=75, tick_size=0.05)
    assert inst.is_option
    assert inst.instrument_type is InstrumentType.OPTION
    assert inst.exchange is Exchange.NFO
    assert inst.underlying == "NIFTY"
    assert inst.strike == 25100.0
    assert inst.option_type == "CE"
    assert inst.lot_size == 75 and inst.tick_size == 0.05
    assert inst.internal_symbol == "NIFTY|2026-09-03|25100|CE"


@pytest.mark.parametrize("alias", ["FINNIFTY", "NIFTY_FIN_SERVICE", "NiftyFinService"])
def test_finnifty_alias_resolution(alias):
    assert normalize_index_symbol(alias) == "FINNIFTY"
    inst = index_instrument(alias)
    assert inst.internal_symbol == "FINNIFTY"
    assert inst.exchange is Exchange.NSE
    assert inst.instrument_type is InstrumentType.INDEX


def test_equity_instrument_generic_resolution():
    """Section 27: any valid NSE symbol resolves, not just a hardcoded list."""
    inst = equity_instrument("reliance")
    assert inst.internal_symbol == "RELIANCE"
    assert inst.exchange is Exchange.NSE
    assert inst.instrument_type is InstrumentType.EQUITY


def test_equity_instrument_rejects_invalid_symbol():
    with pytest.raises(ValueError):
        equity_instrument("bad symbol!")
    with pytest.raises(ValueError):
        equity_instrument("")
