"""Regression: ICICI's real BFO security-master rows carry
underlying="BSESEN" (their internal short-code), not "SENSEX" -- a raw
string-equality match against the requested underlying silently dropped
every SENSEX row, while NFO/NIFTY rows (underlying="NIFTY" already)
happened to work. get_option_instruments() must normalize through the
same code->internal-symbol mapping used for ticks (_tick_symbol_to_internal)
before comparing."""
from __future__ import annotations

from trading.market_data.providers.icici_breeze import ICICIBreezeProvider

_ROWS = [
    {"underlying": "NIFTY", "expiry": "08-Sep-2026", "strike": "24000", "option_type": "CE",
     "token": "1", "lot_size": "75", "tick_size": "0.05", "exchange": "NFO", "symbol": "NIFTY"},
    {"underlying": "NIFTY", "expiry": "08-Sep-2026", "strike": "24000", "option_type": "PE",
     "token": "2", "lot_size": "75", "tick_size": "0.05", "exchange": "NFO", "symbol": "NIFTY"},
    # real ICICI BFO rows: "underlying" is their short-code "BSESEN", the
    # human-readable name only shows up in "symbol".
    {"underlying": "BSESEN", "expiry": "24-Sep-2026", "strike": "81250", "option_type": "CE",
     "token": "3", "lot_size": "20", "tick_size": "5", "exchange": "BFO", "symbol": "SENSEX"},
    {"underlying": "BSESEN", "expiry": "24-Sep-2026", "strike": "81250", "option_type": "PE",
     "token": "4", "lot_size": "20", "tick_size": "5", "exchange": "BFO", "symbol": "SENSEX"},
]


def _provider() -> ICICIBreezeProvider:
    return ICICIBreezeProvider(
        api_key="k", api_secret="s", session_token="t", master_loader=lambda: _ROWS,
    )


def test_sensex_option_instruments_are_not_dropped():
    provider = _provider()
    insts = provider.get_option_instruments("SENSEX")
    assert len(insts) == 2
    assert {i.option_type for i in insts} == {"CE", "PE"}
    assert all(i.underlying == "SENSEX" for i in insts)
    assert all(i.exchange.value == "BFO" for i in insts)
    assert insts[0].strike == 81250.0


def test_nifty_option_instruments_still_work():
    provider = _provider()
    insts = provider.get_option_instruments("NIFTY")
    assert len(insts) == 2
    assert all(i.underlying == "NIFTY" for i in insts)
    assert all(i.exchange.value == "NFO" for i in insts)


def test_sensex_and_nifty_never_cross_contaminate():
    provider = _provider()
    nifty = provider.get_option_instruments("NIFTY")
    sensex = provider.get_option_instruments("SENSEX")
    assert {i.provider_token for i in nifty}.isdisjoint({i.provider_token for i in sensex})
