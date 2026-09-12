"""trading/common/instrument.py -- Instrument is pure data, broker-independent."""
from __future__ import annotations

from trading.common.instrument import Instrument


def test_minimal_construction_defaults():
    inst = Instrument(symbol="NIFTY15SEP2623950CE", exchange="NFO")
    assert inst.symbol == "NIFTY15SEP2623950CE"
    assert inst.exchange == "NFO"
    assert inst.instrument_id == ""
    assert inst.underlying == ""
    assert inst.expiry == ""
    assert inst.strike is None
    assert inst.option_type == ""
    assert inst.metadata == {}


def test_is_option_true_for_ce_and_pe():
    ce = Instrument(symbol="NIFTY15SEP2623950CE", exchange="NFO", option_type="CE")
    pe = Instrument(symbol="NIFTY15SEP2623950PE", exchange="NFO", option_type="PE")
    assert ce.is_option is True
    assert pe.is_option is True


def test_is_option_false_for_equity():
    eq = Instrument(symbol="RELIANCE", exchange="NSE")
    assert eq.is_option is False


def test_full_option_construction():
    inst = Instrument(
        symbol="NIFTY15SEP2623950CE", exchange="NFO", underlying="NIFTY",
        expiry="2026-09-15", strike=23950.0, option_type="CE",
    )
    assert inst.underlying == "NIFTY"
    assert inst.expiry == "2026-09-15"
    assert inst.strike == 23950.0


def test_metadata_is_independent_per_instance():
    a = Instrument(symbol="A", exchange="NFO")
    b = Instrument(symbol="B", exchange="NFO")
    a.metadata["x"] = 1
    assert b.metadata == {}


def test_has_no_broker_specific_field_names():
    forbidden = {"smartapi", "breeze", "dhan", "shoonya", "symboltoken", "variety", "producttype"}
    field_names = {f.lower() for f in Instrument.__dataclass_fields__.keys()}
    assert field_names.isdisjoint(forbidden)
