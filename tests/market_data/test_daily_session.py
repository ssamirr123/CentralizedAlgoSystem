"""Straddle Pulse -- daily session: one per (underlying, trading_date),
never created on a non-trading day, idempotent."""
from __future__ import annotations

from datetime import date

from trading.database import models
from trading.market_data.daily_session import DailySessionService
from trading.market_data.expiry_cycle import ExpiryCycleService


def _cycle(db_session, underlying="NIFTY", expiry=date(2026, 9, 8)):
    return ExpiryCycleService().get_or_create_active_cycle(db_session, underlying, [expiry], date(2026, 9, 2))


def test_no_session_on_weekend(db_session):
    svc = DailySessionService()
    cycle = _cycle(db_session)
    saturday = date(2026, 9, 5)
    assert svc.get_or_create_today(db_session, "NIFTY", cycle, saturday) is None
    assert db_session.query(models.DailySession).count() == 0


def test_no_session_on_configured_holiday(db_session):
    svc = DailySessionService()
    cycle = _cycle(db_session)
    holiday = date(2026, 9, 3)
    assert svc.get_or_create_today(db_session, "NIFTY", cycle, holiday, holidays={holiday}) is None


def test_session_created_on_trading_day_and_idempotent(db_session):
    svc = DailySessionService()
    cycle = _cycle(db_session)
    day = date(2026, 9, 3)
    first = svc.get_or_create_today(db_session, "NIFTY", cycle, day)
    second = svc.get_or_create_today(db_session, "NIFTY", cycle, day)
    assert first is not None and first.id == second.id
    assert first.session_status == "PENDING"
    assert db_session.query(models.DailySession).count() == 1


def test_sessions_independent_across_underlyings(db_session):
    svc = DailySessionService()
    nifty_cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    sensex_cycle = _cycle(db_session, "SENSEX", date(2026, 9, 10))
    day = date(2026, 9, 3)

    n = svc.get_or_create_today(db_session, "NIFTY", nifty_cycle, day)
    s = svc.get_or_create_today(db_session, "SENSEX", sensex_cycle, day)

    assert n.id != s.id
    assert n.underlying == "NIFTY" and s.underlying == "SENSEX"


def test_session_references_cycle_correctly(db_session):
    """cycle_id must resolve back to a cycle whose underlying and expiry
    actually match the session -- the FK relationship, not just a
    dangling integer."""
    svc = DailySessionService()
    cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    session = svc.get_or_create_today(db_session, "NIFTY", cycle, date(2026, 9, 3))

    assert session.cycle_id == cycle.id
    assert session.cycle.id == cycle.id
    assert session.cycle.underlying == session.underlying == "NIFTY"
    assert session.cycle.expiry_date == date(2026, 9, 8)

    # querying sessions by cycle_id returns only this underlying's rows
    rows = db_session.query(models.DailySession).filter_by(cycle_id=cycle.id).all()
    assert [r.id for r in rows] == [session.id]
