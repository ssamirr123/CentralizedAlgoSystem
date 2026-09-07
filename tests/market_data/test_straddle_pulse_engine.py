"""Straddle Pulse -- Phase 3: expiry detection + cycle lifecycle, driven
end-to-end through StraddlePulseEngine (the real recover()/tick() entry
points MarketDataService calls), not just the isolated services.

Expiry always comes from InstrumentMaster.list_expiries() (the real
instrument-master source) -- never a hardcoded weekday -- and NIFTY /
SENSEX are independent throughout.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from trading.core.config import Settings
from trading.database import models
from trading.database.connection import SessionLocal
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.straddle_pulse import StraddlePulseEngine
from trading.market_data.symbols import option_instrument

IST = ZoneInfo("Asia/Kolkata")


def _master(underlying: str, expiries: list[date]) -> InstrumentMaster:
    m = InstrumentMaster()
    insts = [
        option_instrument(underlying, exp, k, ot, lot_size=75, provider="icici_breeze", provider_token=f"t{exp}{k}{ot}")
        for exp in expiries
        for k in range(23800, 24201, 50)
        for ot in ("CE", "PE")
    ]
    m.load(insts, as_of=date(2026, 9, 1))
    return m


def _multi_underlying_master(nifty_expiries: list[date], sensex_expiries: list[date]) -> InstrumentMaster:
    m = InstrumentMaster()
    insts = []
    for underlying, expiries in (("NIFTY", nifty_expiries), ("SENSEX", sensex_expiries)):
        for exp in expiries:
            for k in range(23800, 24201, 50):
                for ot in ("CE", "PE"):
                    insts.append(option_instrument(underlying, exp, k, ot, lot_size=75,
                                                    provider="icici_breeze", provider_token=f"t{underlying}{exp}{k}{ot}"))
    m.load(insts, as_of=date(2026, 9, 1))
    return m


def _engine(master: InstrumentMaster, *, holidays: str = "") -> StraddlePulseEngine:
    settings = Settings(market_data_timezone="Asia/Kolkata", market_data_holidays=holidays)
    return StraddlePulseEngine(master=master, cache=LiveCache(), session_factory=SessionLocal, settings=settings)


def _active_cycle(underlying: str) -> models.ExpiryCycle | None:
    db = SessionLocal()
    try:
        return (
            db.query(models.ExpiryCycle)
            .filter_by(underlying=underlying, status="ACTIVE")
            .one_or_none()
        )
    finally:
        db.close()


def test_1_normal_nifty_cycle_from_real_expiry_source(db_session):
    master = _master("NIFTY", [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)])
    engine = _engine(master)
    engine.tick("NIFTY", datetime(2026, 9, 3, 9, 0, tzinfo=IST))

    cycle = _active_cycle("NIFTY")
    assert cycle is not None
    assert cycle.expiry_date == date(2026, 9, 8)
    assert cycle.cycle_start_date == date(2026, 9, 2)  # day after the previous listed expiry


def test_2_normal_sensex_cycle_from_real_expiry_source(db_session):
    master = _master("SENSEX", [date(2026, 8, 28), date(2026, 9, 10), date(2026, 9, 17)])
    engine = _engine(master)
    engine.tick("SENSEX", datetime(2026, 9, 3, 9, 0, tzinfo=IST))

    cycle = _active_cycle("SENSEX")
    assert cycle is not None
    assert cycle.expiry_date == date(2026, 9, 10)
    assert cycle.cycle_start_date == date(2026, 8, 29)


def test_3_different_expiry_schedules_are_never_shared(db_session):
    """NIFTY and SENSEX get different expiry dates and different cycle
    lengths from the same run -- no weekday assumption ties them together."""
    master = _multi_underlying_master(
        nifty_expiries=[date(2026, 9, 1), date(2026, 9, 8)],
        sensex_expiries=[date(2026, 8, 28), date(2026, 9, 10)],  # 2 days longer than NIFTY's
    )
    engine = _engine(master)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=IST)
    engine.tick("NIFTY", now)
    engine.tick("SENSEX", now)

    nifty, sensex = _active_cycle("NIFTY"), _active_cycle("SENSEX")
    assert nifty.expiry_date != sensex.expiry_date
    nifty_len = (nifty.cycle_end_date - nifty.cycle_start_date).days
    sensex_len = (sensex.cycle_end_date - sensex.cycle_start_date).days
    assert nifty_len != sensex_len


def test_4_holiday_gap_leaves_cycle_boundary_intact_but_skips_session(db_session):
    """The cycle's cycle_start_date is a pure calendar-day calculation
    (day after the previous expiry) and must not shift just because that
    day happens to be a holiday; the daily *session* for that day is what
    gets skipped."""
    holiday = date(2026, 9, 2)  # the would-be cycle start
    master = _master("NIFTY", [date(2026, 9, 1), date(2026, 9, 8)])
    engine = _engine(master, holidays=holiday.isoformat())

    engine.tick("NIFTY", datetime(2026, 9, 2, 9, 0, tzinfo=IST))  # tick ON the holiday

    cycle = _active_cycle("NIFTY")
    assert cycle.cycle_start_date == holiday  # boundary unaffected by the holiday

    db = SessionLocal()
    try:
        sessions = db.query(models.DailySession).filter_by(underlying="NIFTY", trading_date=holiday).all()
        assert sessions == []  # no fake session created for the holiday
    finally:
        db.close()

    # the next trading day still gets a session, in the same cycle
    engine.tick("NIFTY", datetime(2026, 9, 3, 9, 0, tzinfo=IST))
    db = SessionLocal()
    try:
        s = db.query(models.DailySession).filter_by(underlying="NIFTY", trading_date=date(2026, 9, 3)).one()
        assert s.cycle_id == cycle.id
    finally:
        db.close()


def test_5_rollover_completes_old_cycle_and_activates_next(db_session):
    master = _master("NIFTY", [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)])
    engine = _engine(master)

    engine.tick("NIFTY", datetime(2026, 9, 3, 9, 0, tzinfo=IST))
    first_cycle_id = _active_cycle("NIFTY").id

    # roll past the first expiry
    engine.tick("NIFTY", datetime(2026, 9, 9, 9, 0, tzinfo=IST))

    db = SessionLocal()
    try:
        old = db.get(models.ExpiryCycle, first_cycle_id)
        assert old.status == "COMPLETED"
    finally:
        db.close()

    new = _active_cycle("NIFTY")
    assert new is not None
    assert new.id != first_cycle_id
    assert new.expiry_date == date(2026, 9, 15)


def test_6_repeated_rollover_produces_no_duplicates(db_session):
    """Ticking the engine many times across a rollover boundary must
    still leave exactly one ACTIVE + the right COMPLETED history --
    never duplicate cycles."""
    master = _master("NIFTY", [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15), date(2026, 9, 22)])
    engine = _engine(master)

    for _ in range(3):
        engine.tick("NIFTY", datetime(2026, 9, 3, 9, 0, tzinfo=IST))
    for _ in range(3):
        engine.tick("NIFTY", datetime(2026, 9, 9, 9, 0, tzinfo=IST))  # past cycle 1
    for _ in range(3):
        engine.tick("NIFTY", datetime(2026, 9, 16, 9, 0, tzinfo=IST))  # past cycle 2

    db = SessionLocal()
    try:
        rows = db.query(models.ExpiryCycle).filter_by(underlying="NIFTY").order_by(models.ExpiryCycle.expiry_date).all()
    finally:
        db.close()
    assert [r.expiry_date for r in rows] == [date(2026, 9, 8), date(2026, 9, 15), date(2026, 9, 22)]
    assert [r.status for r in rows] == ["COMPLETED", "COMPLETED", "ACTIVE"]


def test_7_backend_restart_recovery_creates_no_duplicates(db_session):
    """A fresh StraddlePulseEngine (simulating a process restart, pointed
    at the same DB/instrument master) calling recover() must find the
    existing cycle/session rather than duplicating them."""
    master = _master("NIFTY", [date(2026, 9, 1), date(2026, 9, 8)])
    restart_at = datetime(2026, 9, 3, 9, 17, tzinfo=IST)
    engine1 = _engine(master)
    engine1.tick("NIFTY", restart_at)

    cycle_before = _active_cycle("NIFTY")
    db = SessionLocal()
    try:
        session_before = db.query(models.DailySession).filter_by(underlying="NIFTY", trading_date=date(2026, 9, 3)).one()
        session_before_id = session_before.id
    finally:
        db.close()

    # simulate restart: brand-new engine instance, same DB/master, process
    # comes back up moments later on the same trading day.
    engine2 = _engine(master)
    engine2.recover("NIFTY", datetime(2026, 9, 3, 9, 20, tzinfo=IST))

    db = SessionLocal()
    try:
        assert db.query(models.ExpiryCycle).filter_by(underlying="NIFTY").count() == 1
        assert db.query(models.DailySession).filter_by(underlying="NIFTY").count() == 1
        session_after = db.query(models.DailySession).filter_by(underlying="NIFTY", trading_date=date(2026, 9, 3)).one()
        assert session_after.id == session_before_id
        assert session_after.cycle_id == cycle_before.id
    finally:
        db.close()


def test_8_historical_cycle_remains_intact_after_rollover(db_session):
    master = _master("NIFTY", [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)])
    engine = _engine(master)

    engine.tick("NIFTY", datetime(2026, 9, 3, 9, 17, tzinfo=IST))
    old_cycle_id = _active_cycle("NIFTY").id
    db = SessionLocal()
    try:
        old_session_id = (
            db.query(models.DailySession).filter_by(underlying="NIFTY", trading_date=date(2026, 9, 3)).one().id
        )
    finally:
        db.close()

    engine.tick("NIFTY", datetime(2026, 9, 9, 9, 0, tzinfo=IST))  # rollover

    db = SessionLocal()
    try:
        old_cycle = db.get(models.ExpiryCycle, old_cycle_id)
        old_session = db.get(models.DailySession, old_session_id)
        assert old_cycle is not None and old_cycle.status == "COMPLETED"
        assert old_session is not None  # never deleted
        assert old_session.trading_date == date(2026, 9, 3)
        assert old_session.cycle_id == old_cycle_id
    finally:
        db.close()


def test_9_duplicate_cycle_creation_prevented_across_repeated_ticks(db_session):
    master = _multi_underlying_master(
        nifty_expiries=[date(2026, 9, 1), date(2026, 9, 8)],
        sensex_expiries=[date(2026, 8, 28), date(2026, 9, 10)],
    )
    engine = _engine(master)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=IST)

    for _ in range(5):
        engine.tick("NIFTY", now)
        engine.tick("SENSEX", now)

    db = SessionLocal()
    try:
        assert db.query(models.ExpiryCycle).filter_by(underlying="NIFTY").count() == 1
        assert db.query(models.ExpiryCycle).filter_by(underlying="SENSEX").count() == 1
    finally:
        db.close()
