"""Phase 6 -- NIFTY option chain: ATM, strike range, expiry selection."""
from __future__ import annotations

from datetime import date, datetime, timezone

from trading.market_data.cache import LiveCache
from trading.market_data import option_chain as oc
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.schemas import OptionQuote
from trading.market_data.symbols import make_option_symbol, option_instrument

E1 = date(2100, 9, 3)   # far future so "current" resolution is deterministic
E2 = date(2100, 9, 10)
STEP = 50


def _master():
    m = InstrumentMaster()
    insts = []
    for exp in (E1, E2):
        for k in range(24000, 26001, STEP):
            for ot in ("CE", "PE"):
                insts.append(option_instrument("NIFTY", exp, k, ot, lot_size=75, tick_size=0.05))
    m.load(insts, as_of=date(2100, 9, 1))
    return m


def test_strike_step_derived_not_hardcoded():
    assert oc.strike_step([24000, 24050, 24100, 24200]) == 50


def test_nearest_strike():
    assert oc.nearest_strike([24950, 25000, 25050], 25012) == 25000
    assert oc.nearest_strike([], 100) is None


def test_select_strike_window_atm_plus_minus_n():
    strikes = list(range(24000, 26001, 50))
    win = oc.select_strike_window(strikes, 25000, 3)
    assert win == [24850, 24900, 24950, 25000, 25050, 25100, 25150]


def test_resolve_expiry_current_next_specific():
    m = _master()
    assert oc.resolve_expiry(m, "NIFTY", "current") == E1
    assert oc.resolve_expiry(m, "NIFTY", "next") == E2
    assert oc.resolve_expiry(m, "NIFTY", E2) == E2
    assert oc.resolve_expiry(m, "NIFTY", "2100-09-10") == E2


def test_build_chain_atm_from_spot_and_range():
    m = _master()
    cache = LiveCache()
    # seed a live quote for the 25000 CE
    q = OptionQuote.build(underlying="NIFTY", expiry=E1, strike=25000, option_type="CE",
                          ltp=151.0, oi=123456, provider="icici_breeze")
    cache.put(q)

    chain = oc.build_option_chain(
        underlying="NIFTY", expiry=E1, spot=25012.0, cache=cache, master=m, strike_range=2,
    )
    assert chain.atm_strike == 25000
    assert [r.strike for r in chain.rows] == [24900, 24950, 25000, 25050, 25100]
    atm = next(r for r in chain.rows if r.strike == 25000)
    assert atm.call is not None and atm.call.ltp == 151.0
    assert atm.put is None  # no live put quote -> null, not fabricated
    # a strike with no quotes at all
    assert chain.rows[0].call is None and chain.rows[0].put is None


def test_build_chain_no_spot_returns_full_list_no_atm():
    m = _master()
    chain = oc.build_option_chain(
        underlying="NIFTY", expiry=E1, spot=None, cache=LiveCache(), master=m, strike_range=5,
    )
    assert chain.atm_strike is None
    assert len(chain.rows) == len(m.list_strikes("NIFTY", E1))
