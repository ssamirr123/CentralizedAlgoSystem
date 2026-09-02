"""Phase 5 -- instrument master + Breeze get_option_instruments."""
from __future__ import annotations

from datetime import date

import pytest

from trading.market_data.instruments import InstrumentMaster
from trading.market_data.providers import ICICIBreezeProvider, ProviderConnectionError
from trading.market_data.symbols import Exchange, InstrumentType, make_option_symbol, option_instrument

E1 = date(2026, 9, 3)
E2 = date(2026, 9, 10)


def _universe():
    out = []
    for exp in (E1, E2):
        for strike in (24900, 25000, 25100):
            for ot in ("CE", "PE"):
                out.append(option_instrument(
                    "NIFTY", exp, strike, ot, lot_size=75, tick_size=0.05,
                    provider="icici_breeze", provider_token=f"NFO-{exp}-{strike}-{ot}",
                ))
    return out


# --- InstrumentMaster ---------------------------------------------------
def test_load_and_resolve():
    m = InstrumentMaster()
    n = m.load(_universe(), as_of=date(2026, 9, 1))
    assert n == 12
    assert m.count("NIFTY") == 12
    assert m.count() == 12

    inst = m.resolve("nifty", E1, 25000, "ce")
    assert inst is not None
    assert inst.instrument_type is InstrumentType.OPTION
    assert inst.exchange is Exchange.NFO
    assert inst.lot_size == 75 and inst.tick_size == 0.05
    assert inst.provider == "icici_breeze"
    assert inst.provider_token == f"NFO-{E1}-25000-CE"
    assert inst.internal_symbol == make_option_symbol("NIFTY", E1, 25000, "CE")

    assert m.resolve("NIFTY", E1, 99999, "CE") is None
    assert m.get(inst.internal_symbol) is inst


def test_list_expiries_and_strikes_sorted():
    m = InstrumentMaster()
    m.load(_universe())
    assert m.list_expiries("NIFTY") == [E1, E2]
    assert m.list_strikes("NIFTY", E1) == [24900.0, 25000.0, 25100.0]
    assert m.list_strikes("NIFTY", date(2030, 1, 1)) == []


def test_needs_refresh_tracks_as_of_date():
    m = InstrumentMaster()
    assert m.is_empty() and m.needs_refresh(date(2026, 9, 7)) is True
    m.load(_universe(), as_of=date(2026, 9, 7))
    assert m.needs_refresh(date(2026, 9, 7)) is False
    assert m.needs_refresh(date(2026, 9, 8)) is True


def test_load_ignores_non_option_and_malformed():
    from trading.market_data.symbols import index_instrument

    m = InstrumentMaster()
    good = option_instrument("NIFTY", E1, 25000, "CE")
    n = m.load([good, index_instrument("NIFTY")])  # index dropped
    assert n == 1


def test_refresh_pulls_from_provider():
    class FakeProv:
        name = "icici_breeze"

        def get_option_instruments(self, underlying):
            assert underlying == "NIFTY"
            return _universe()

    m = InstrumentMaster()
    n = m.refresh(FakeProv(), ("NIFTY",), as_of=date(2026, 9, 7))
    assert n == 12
    assert m.last_source == "icici_breeze"
    assert m.needs_refresh(date(2026, 9, 7)) is False


# --- ICICIBreezeProvider.get_option_instruments -----------------------
_MASTER_ROWS = [
    {"underlying": "NIFTY", "expiry": "2026-09-03", "strike": "25000", "option_type": "CE",
     "token": "12345", "lot_size": "75", "tick_size": "0.05", "exchange": "NFO"},
    {"underlying": "NIFTY", "expiry": "03-Sep-2026", "strike": "25000", "option_type": "PE",
     "token": "12346", "lot_size": "75", "tick_size": "0.05", "exchange": "NFO"},
    {"underlying": "NIFTY", "expiry": "2026-09-10", "strike": "25100", "right": "Call",
     "token": "22345", "lot_size": "75", "tick_size": "0.05"},
    {"underlying": "BANKNIFTY", "expiry": "2026-09-03", "strike": "52000", "option_type": "CE",
     "token": "99999", "lot_size": "35"},               # different underlying -> filtered out
    {"underlying": "NIFTY", "strike": "25000", "option_type": "CE", "token": "x"},  # no expiry -> skipped
    {"garbage": True},                                                              # skipped
]


def _breeze(rows):
    return ICICIBreezeProvider(
        api_key="k", api_secret="s", session_token="t",
        client_factory=lambda ak: object(),
        master_loader=lambda: list(rows),
    )


def test_get_option_instruments_parses_and_filters():
    p = _breeze(_MASTER_ROWS)
    insts = p.get_option_instruments("nifty")
    assert len(insts) == 3
    syms = {i.internal_symbol for i in insts}
    assert make_option_symbol("NIFTY", E1, 25000, "CE") in syms
    assert make_option_symbol("NIFTY", E1, 25000, "PE") in syms
    assert make_option_symbol("NIFTY", E2, 25100, "CE") in syms

    ce = next(i for i in insts if i.internal_symbol.endswith("25000|CE"))
    assert ce.provider_token == "12345"
    assert ce.lot_size == 75 and ce.tick_size == 0.05
    assert ce.exchange is Exchange.NFO
    assert ce.underlying == "NIFTY" and ce.expiry == E1

    # feed straight into the master
    m = InstrumentMaster()
    m.load(insts, as_of=date(2026, 9, 7))
    assert m.list_expiries("NIFTY") == [E1, E2]
    assert m.resolve("NIFTY", E2, 25100, "CE").provider_token == "22345"


def test_get_option_instruments_loader_failure_is_connection_error():
    def boom():
        raise TimeoutError("master download timed out")

    p = ICICIBreezeProvider(api_key="k", api_secret="s", session_token="t", master_loader=boom)
    with pytest.raises(ProviderConnectionError):
        p.get_option_instruments("NIFTY")
