"""Straddle Pulse -- intraday OI build-up + PCR: scoped by
underlying+expiry+trading_date+timestamp, PCR = put_oi / call_oi."""
from __future__ import annotations

from datetime import date, datetime, timezone

from trading.database import models
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.oi_pcr import OIService
from trading.market_data.schemas import OptionQuote
from trading.market_data.symbols import option_instrument

EXPIRY = date(2026, 9, 8)


def _master():
    m = InstrumentMaster()
    m.load(
        [
            option_instrument("NIFTY", EXPIRY, k, ot, lot_size=75, provider="icici_breeze", provider_token=f"t{k}{ot}")
            for k in range(23950, 24101, 50)
            for ot in ("CE", "PE")
        ],
        as_of=date(2026, 9, 1),
    )
    return m


def _locked_session(db_session, atm=24050.0, underlying="NIFTY", expiry=EXPIRY,
                     exchange="NFO", trading_date=date(2026, 9, 3), status="ACTIVE"):
    cycle = (
        db_session.query(models.ExpiryCycle)
        .filter_by(underlying=underlying, expiry_date=expiry)
        .one_or_none()
    )
    if cycle is None:
        cycle = models.ExpiryCycle(
            underlying=underlying, exchange=exchange, expiry_date=expiry,
            cycle_start_date=date(2026, 9, 2), cycle_end_date=expiry, status=status,
        )
        db_session.add(cycle)
        db_session.commit()
    session = models.DailySession(
        cycle_id=cycle.id, underlying=underlying, trading_date=trading_date,
        atm_strike=atm, session_status="LOCKED",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def _master_for(underlying: str, expiry: date, base: int = 24000):
    m = InstrumentMaster()
    m.load(
        [
            option_instrument(underlying, expiry, k, ot, lot_size=75,
                              provider="icici_breeze", provider_token=f"t{underlying}{expiry}{k}{ot}")
            for k in range(base - 100, base + 101, 50)
            for ot in ("CE", "PE")
        ],
        as_of=date(2026, 9, 1),
    )
    return m


def _cache_with_oi(call_oi=100000, put_oi=50000, underlying="NIFTY", expiry=EXPIRY, strike=24050):
    cache = LiveCache()
    cache.put(OptionQuote.build(underlying=underlying, expiry=expiry, strike=strike, option_type="CE",
                                ltp=100.0, oi=call_oi, provider="icici_breeze"))
    cache.put(OptionQuote.build(underlying=underlying, expiry=expiry, strike=strike, option_type="PE",
                                ltp=90.0, oi=put_oi, provider="icici_breeze"))
    return cache


def test_no_snapshot_before_atm_locked(db_session):
    session = models.DailySession(
        cycle_id=1, underlying="NIFTY", trading_date=date(2026, 9, 3), session_status="PENDING",
    )
    assert OIService().snapshot(db_session, session, _master(), LiveCache(), strike_range=1) is None


def test_pcr_formula_and_scoping(db_session):
    session = _locked_session(db_session)
    row = OIService().snapshot(
        db_session, session, _master(), _cache_with_oi(call_oi=100000, put_oi=50000), strike_range=1,
        now_utc=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
    )
    assert row.underlying == "NIFTY"
    assert row.cycle_id == session.cycle_id
    assert row.expiry_date == EXPIRY
    assert row.trading_date == date(2026, 9, 3)
    assert row.call_oi_total == 100000
    assert row.put_oi_total == 50000
    assert row.pcr == 0.5


def test_repeated_snapshot_same_minute_is_idempotent(db_session):
    session = _locked_session(db_session)
    svc = OIService()
    ts = datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc)
    first = svc.snapshot(db_session, session, _master(), _cache_with_oi(), strike_range=1, now_utc=ts)
    second = svc.snapshot(db_session, session, _master(), _cache_with_oi(), strike_range=1, now_utc=ts)
    assert first.id == second.id
    assert db_session.query(models.OISnapshot).count() == 1


def test_oi_change_is_relative_to_first_snapshot_of_day(db_session):
    session = _locked_session(db_session)
    svc = OIService()
    svc.snapshot(
        db_session, session, _master(), _cache_with_oi(call_oi=100000, put_oi=50000), strike_range=1,
        now_utc=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
    )
    later = svc.snapshot(
        db_session, session, _master(), _cache_with_oi(call_oi=142300, put_oi=58150), strike_range=1,
        now_utc=datetime(2026, 9, 3, 4, 30, tzinfo=timezone.utc),
    )
    assert later.call_oi_change == 42300
    assert later.put_oi_change == 8150


def test_sensex_oi_independent_of_nifty(db_session):
    sensex_expiry = date(2026, 9, 10)
    session = _locked_session(db_session, atm=81250.0, underlying="SENSEX", expiry=sensex_expiry, exchange="BFO")
    master = _master_for("SENSEX", sensex_expiry, base=81250)
    cache = _cache_with_oi(call_oi=200000, put_oi=90000, underlying="SENSEX", expiry=sensex_expiry, strike=81250)

    row = OIService().snapshot(
        db_session, session, master, cache, strike_range=1,
        now_utc=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
    )
    assert row.underlying == "SENSEX"
    assert row.call_oi_total == 200000
    assert row.put_oi_total == 90000
    assert row.pcr == 0.45
    assert db_session.query(models.OISnapshot).filter_by(underlying="NIFTY").count() == 0


def test_expiry_association_stored_on_row(db_session):
    session = _locked_session(db_session)
    row = OIService().snapshot(
        db_session, session, _master(), _cache_with_oi(), strike_range=1,
        now_utc=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
    )
    assert row.expiry_date == session.cycle.expiry_date == EXPIRY


def test_cycle_association_stored_and_correct(db_session):
    session = _locked_session(db_session)
    row = OIService().snapshot(
        db_session, session, _master(), _cache_with_oi(), strike_range=1,
        now_utc=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
    )
    assert row.cycle_id == session.cycle_id
    assert row.cycle_id == session.cycle.id


def test_cross_underlying_isolation(db_session):
    sensex_expiry = date(2026, 9, 10)
    nifty_session = _locked_session(db_session)
    sensex_session = _locked_session(db_session, atm=81250.0, underlying="SENSEX", expiry=sensex_expiry, exchange="BFO")

    OIService().snapshot(
        db_session, nifty_session, _master(), _cache_with_oi(call_oi=100000, put_oi=50000), strike_range=1,
        now_utc=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
    )
    OIService().snapshot(
        db_session, sensex_session, _master_for("SENSEX", sensex_expiry, base=81250),
        _cache_with_oi(call_oi=200000, put_oi=90000, underlying="SENSEX", expiry=sensex_expiry, strike=81250),
        strike_range=1, now_utc=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
    )

    nifty_rows = db_session.query(models.OISnapshot).filter_by(underlying="NIFTY").all()
    sensex_rows = db_session.query(models.OISnapshot).filter_by(underlying="SENSEX").all()
    assert len(nifty_rows) == 1 and nifty_rows[0].call_oi_total == 100000
    assert len(sensex_rows) == 1 and sensex_rows[0].call_oi_total == 200000
    assert nifty_rows[0].cycle_id != sensex_rows[0].cycle_id


def test_cross_expiry_isolation(db_session):
    """Two cycles for the SAME underlying (a completed one and the new
    active one, as happens across rollover) must keep separate OI rows,
    never merged even though underlying matches."""
    old_expiry = EXPIRY  # 2026-09-08
    new_expiry = date(2026, 9, 15)

    old_session = _locked_session(db_session, expiry=old_expiry, trading_date=date(2026, 9, 3), status="COMPLETED")
    new_session = _locked_session(db_session, atm=24100.0, expiry=new_expiry, trading_date=date(2026, 9, 10))

    old_master = _master_for("NIFTY", old_expiry)
    new_master = _master_for("NIFTY", new_expiry)

    OIService().snapshot(
        db_session, old_session, old_master, _cache_with_oi(call_oi=100000, put_oi=50000, expiry=old_expiry),
        strike_range=1, now_utc=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
    )
    OIService().snapshot(
        db_session, new_session, new_master,
        _cache_with_oi(call_oi=300000, put_oi=120000, expiry=new_expiry, strike=24100),
        strike_range=1, now_utc=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc),
    )

    old_rows = db_session.query(models.OISnapshot).filter_by(expiry_date=old_expiry).all()
    new_rows = db_session.query(models.OISnapshot).filter_by(expiry_date=new_expiry).all()
    assert len(old_rows) == 1 and old_rows[0].call_oi_total == 100000
    assert len(new_rows) == 1 and new_rows[0].call_oi_total == 300000
    assert old_rows[0].cycle_id != new_rows[0].cycle_id
    # historical row's cycle is COMPLETED but its OI data is untouched
    assert old_session.cycle.status == "COMPLETED"
    assert old_rows[0].put_oi_total == 50000
