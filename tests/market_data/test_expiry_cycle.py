"""Straddle Pulse -- expiry-cycle identity: expiry defines the cycle,
never a calendar week; NIFTY and SENSEX cycles are fully independent."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from trading.database import models
from trading.market_data.expiry_cycle import ExpiryCycleService


def test_nifty_and_sensex_cycles_are_independent(db_session):
    svc = ExpiryCycleService()
    today = date(2026, 9, 2)

    nifty_expiries = [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)]
    sensex_expiries = [date(2026, 8, 28), date(2026, 9, 10), date(2026, 9, 17)]

    nifty_cycle = svc.get_or_create_active_cycle(db_session, "NIFTY", nifty_expiries, today)
    sensex_cycle = svc.get_or_create_active_cycle(db_session, "SENSEX", sensex_expiries, today)

    assert nifty_cycle.expiry_date == date(2026, 9, 8)
    assert nifty_cycle.cycle_start_date == date(2026, 9, 2)  # day after previous NIFTY expiry
    assert sensex_cycle.expiry_date == date(2026, 9, 10)
    assert sensex_cycle.cycle_start_date == date(2026, 8, 29)  # day after previous SENSEX expiry

    # different expiry dates, different cycle lengths -- never shared
    assert nifty_cycle.id != sensex_cycle.id
    assert nifty_cycle.underlying == "NIFTY"
    assert sensex_cycle.underlying == "SENSEX"


def test_get_or_create_is_idempotent(db_session):
    svc = ExpiryCycleService()
    expiries = [date(2026, 9, 1), date(2026, 9, 8)]
    today = date(2026, 9, 2)

    first = svc.get_or_create_active_cycle(db_session, "NIFTY", expiries, today)
    second = svc.get_or_create_active_cycle(db_session, "NIFTY", expiries, today)
    assert first.id == second.id
    assert db_session.query(models.ExpiryCycle).count() == 1


def test_no_upcoming_expiry_returns_none(db_session):
    svc = ExpiryCycleService()
    past_only = [date(2026, 1, 1)]
    assert svc.get_or_create_active_cycle(db_session, "NIFTY", past_only, date(2026, 9, 2)) is None


def test_complete_past_cycles_is_per_underlying(db_session):
    svc = ExpiryCycleService()
    svc.get_or_create_active_cycle(db_session, "NIFTY", [date(2026, 9, 8)], date(2026, 9, 2))
    svc.get_or_create_active_cycle(db_session, "SENSEX", [date(2026, 9, 10)], date(2026, 9, 2))

    completed = svc.complete_past_cycles(db_session, "NIFTY", date(2026, 9, 9))
    assert completed == 1

    nifty = db_session.query(models.ExpiryCycle).filter_by(underlying="NIFTY").one()
    sensex = db_session.query(models.ExpiryCycle).filter_by(underlying="SENSEX").one()
    assert nifty.status == "COMPLETED"
    assert sensex.status == "ACTIVE"  # untouched -- independent rollover


def test_duplicate_expiry_rejected_at_db_level(db_session):
    """Bypasses the service's get-or-create guard entirely -- the unique
    constraint on (underlying, expiry_date) must reject a raw duplicate
    INSERT, not just rely on service-layer idempotency."""
    row = models.ExpiryCycle(
        underlying="NIFTY", exchange="NFO", expiry_date=date(2026, 9, 8),
        cycle_start_date=date(2026, 9, 2), cycle_end_date=date(2026, 9, 8), status="COMPLETED",
    )
    db_session.add(row)
    db_session.commit()

    dup = models.ExpiryCycle(
        underlying="NIFTY", exchange="NFO", expiry_date=date(2026, 9, 8),
        cycle_start_date=date(2026, 9, 2), cycle_end_date=date(2026, 9, 8), status="ACTIVE",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_only_one_active_cycle_per_underlying_enforced_at_db_level(db_session):
    """DB-level guarantee, not just service-layer ordering: two ACTIVE
    rows for the same underlying (even with different expiry dates) must
    be rejected outright."""
    first = models.ExpiryCycle(
        underlying="NIFTY", exchange="NFO", expiry_date=date(2026, 9, 8),
        cycle_start_date=date(2026, 9, 2), cycle_end_date=date(2026, 9, 8), status="ACTIVE",
    )
    db_session.add(first)
    db_session.commit()

    second_active = models.ExpiryCycle(
        underlying="NIFTY", exchange="NFO", expiry_date=date(2026, 9, 15),
        cycle_start_date=date(2026, 9, 9), cycle_end_date=date(2026, 9, 15), status="ACTIVE",
    )
    db_session.add(second_active)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # a second COMPLETED row for the same underlying is fine -- the
    # constraint only guards ACTIVE.
    second_completed = models.ExpiryCycle(
        underlying="NIFTY", exchange="NFO", expiry_date=date(2026, 9, 15),
        cycle_start_date=date(2026, 9, 9), cycle_end_date=date(2026, 9, 15), status="COMPLETED",
    )
    db_session.add(second_completed)
    db_session.commit()
    assert db_session.query(models.ExpiryCycle).filter_by(underlying="NIFTY").count() == 2

    # a different underlying being ACTIVE at the same time is unaffected.
    sensex_active = models.ExpiryCycle(
        underlying="SENSEX", exchange="BFO", expiry_date=date(2026, 9, 10),
        cycle_start_date=date(2026, 9, 3), cycle_end_date=date(2026, 9, 10), status="ACTIVE",
    )
    db_session.add(sensex_active)
    db_session.commit()


def test_completed_cycle_remains_queryable_with_data_intact(db_session):
    """Rollover flips status; it must never delete the row or touch its
    historical data (spec: no historical data deletion during rollover)."""
    svc = ExpiryCycleService()
    old_expiry = date(2026, 9, 8)
    old = svc.get_or_create_active_cycle(db_session, "NIFTY", [old_expiry], date(2026, 9, 2))
    old_id, old_start = old.id, old.cycle_start_date

    svc.complete_past_cycles(db_session, "NIFTY", date(2026, 9, 9))
    new = svc.get_or_create_active_cycle(db_session, "NIFTY", [old_expiry, date(2026, 9, 15)], date(2026, 9, 9))

    # both rows still present and independently fetchable
    fetched_old = db_session.get(models.ExpiryCycle, old_id)
    assert fetched_old is not None
    assert fetched_old.status == "COMPLETED"
    assert fetched_old.expiry_date == old_expiry
    assert fetched_old.cycle_start_date == old_start  # historical data unchanged
    assert new.status == "ACTIVE"
    assert new.id != old_id
    assert db_session.query(models.ExpiryCycle).filter_by(underlying="NIFTY").count() == 2
