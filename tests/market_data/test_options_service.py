"""Phase 9 -- options acquisition/persistence/point-in-time tests.

Uses an injectable fake provider (no real Breeze credentials needed) for
acquisition tests, and direct DB seeding for persistence/point-in-time
tests (Sections 13, 34, 35, 56, 57)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading.market_data.models import OptionCandle, OptionContract
from trading.market_data.options_service import (
    OptionsDataError,
    get_historical_chain,
    persist_option_chain_snapshot,
)
from trading.market_data.schemas import OptionChain, OptionChainRow, OptionQuote

EXPIRY = date(2026, 9, 29)


def _quote(strike, ot, *, ltp, oi=None, ts=None):
    return OptionQuote.build(
        underlying="NIFTY", expiry=EXPIRY, strike=strike, option_type=ot,
        ltp=ltp, oi=oi, provider_timestamp=ts,
    )


def _chain(rows, *, generated_at):
    return OptionChain(
        underlying="NIFTY", expiry=EXPIRY, spot=25000, atm_strike=25000,
        generated_at=generated_at, rows=rows, provider="test",
    )


def _ts(hour, minute):
    return datetime(2026, 9, 22, hour, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Idempotency (Section 13)
# --------------------------------------------------------------------------
def test_repeated_ingestion_of_identical_snapshot_does_not_duplicate(db_session):
    ts = _ts(10, 0)
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=150, oi=5000, ts=ts), put=_quote(25000, "PE", ltp=100, oi=4000, ts=ts))]
    chain = _chain(rows, generated_at=ts)

    written1 = persist_option_chain_snapshot(db_session, chain)
    written2 = persist_option_chain_snapshot(db_session, chain)

    assert written1 == 2  # one CE + one PE candle created
    assert written2 == 0  # identical snapshot -> updates existing rows, creates none
    assert db_session.query(OptionContract).count() == 2  # CE + PE contracts, not duplicated
    assert db_session.query(OptionCandle).count() == 2


def test_reingestion_with_updated_values_updates_not_duplicates(db_session):
    ts = _ts(10, 0)
    rows_v1 = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=150, oi=5000, ts=ts), put=None)]
    persist_option_chain_snapshot(db_session, _chain(rows_v1, generated_at=ts))

    rows_v2 = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=155, oi=5200, ts=ts), put=None)]
    persist_option_chain_snapshot(db_session, _chain(rows_v2, generated_at=ts))

    assert db_session.query(OptionCandle).count() == 1
    candle = db_session.query(OptionCandle).one()
    assert candle.close == 155
    assert candle.oi == 5200


def test_different_timestamps_create_separate_candles(db_session):
    rows_1000 = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=150, oi=5000, ts=_ts(10, 0)), put=None)]
    rows_1100 = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=160, oi=5100, ts=_ts(11, 0)), put=None)]
    persist_option_chain_snapshot(db_session, _chain(rows_1000, generated_at=_ts(10, 0)))
    persist_option_chain_snapshot(db_session, _chain(rows_1100, generated_at=_ts(11, 0)))
    assert db_session.query(OptionCandle).count() == 2
    assert db_session.query(OptionContract).count() == 1  # same contract, two candle timestamps


# --------------------------------------------------------------------------
# Point-in-time reconstruction (Section 34, 56)
# --------------------------------------------------------------------------
def test_point_in_time_reconstruction_excludes_later_snapshot(db_session):
    for hour, ltp, oi in ((10, 150, 5000), (11, 160, 5200), (12, 170, 5400)):
        rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=ltp, oi=oi, ts=_ts(hour, 0)), put=_quote(25000, "PE", ltp=90, oi=3000, ts=_ts(hour, 0)))]
        persist_option_chain_snapshot(db_session, _chain(rows, generated_at=_ts(hour, 0)))

    result = get_historical_chain(db_session, "NIFTY", EXPIRY, _ts(11, 30))
    call = next(r.call for r in result.chain.rows if r.strike == 25000)
    assert call.ltp == 160  # the 11:00 snapshot, NOT 12:00
    assert result.source == "TIMESCALEDB"
    assert result.provider_call_count == 0


def test_no_future_leakage_even_when_db_has_a_later_row(db_session):
    rows_now = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=150, oi=5000, ts=_ts(10, 0)), put=None)]
    rows_future = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=999, oi=99999, ts=_ts(23, 0)), put=None)]
    persist_option_chain_snapshot(db_session, _chain(rows_now, generated_at=_ts(10, 0)))
    persist_option_chain_snapshot(db_session, _chain(rows_future, generated_at=_ts(23, 0)))

    result = get_historical_chain(db_session, "NIFTY", EXPIRY, _ts(12, 0))
    call = next(r.call for r in result.chain.rows if r.strike == 25000)
    assert call.ltp == 150
    assert call.ltp != 999


def test_historical_chain_missing_when_no_data_before_as_of(db_session):
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=150, oi=5000, ts=_ts(13, 0)), put=None)]
    persist_option_chain_snapshot(db_session, _chain(rows, generated_at=_ts(13, 0)))

    with pytest.raises(OptionsDataError):
        get_historical_chain(db_session, "NIFTY", EXPIRY, _ts(9, 0))  # before any persisted data


def test_historical_chain_unknown_contract_raises(db_session):
    with pytest.raises(OptionsDataError):
        get_historical_chain(db_session, "NIFTY", EXPIRY, _ts(12, 0))


def test_persistence_survives_a_fresh_session(db_session):
    """Simulates a restart: a brand-new Session object must still see the
    committed rows (Section 57), with no Breeze call involved."""
    from trading.database.connection import SessionLocal

    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=150, oi=5000, ts=_ts(10, 0)), put=None)]
    persist_option_chain_snapshot(db_session, _chain(rows, generated_at=_ts(10, 0)))

    fresh = SessionLocal()
    try:
        result = get_historical_chain(fresh, "NIFTY", EXPIRY, _ts(12, 0))
        assert result.chain.rows[0].call.ltp == 150
    finally:
        fresh.close()
