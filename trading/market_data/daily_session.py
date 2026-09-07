"""
Straddle Pulse -- one trading day, one underlying, within one expiry cycle.

No session is created for a non-trading day (weekend or exchange
holiday) -- rule from spec section 27. Session creation is idempotent by
(underlying, trading_date); ATM locking is a separate step (atm_lock.py).
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.market_data import market_hours as mh
from trading.market_data.models import DailySession, ExpiryCycle

logger = logging.getLogger("trading.market_data.daily_session")


class DailySessionService:
    def get_or_create_today(
        self,
        db: Session,
        underlying: str,
        cycle: ExpiryCycle,
        trading_date: date,
        holidays: set[date] | None = None,
    ) -> DailySession | None:
        underlying = underlying.strip().upper()
        if not mh.is_trading_day(trading_date, holidays):
            return None

        existing = db.execute(
            select(DailySession).where(
                DailySession.underlying == underlying, DailySession.trading_date == trading_date,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        row = DailySession(
            cycle_id=cycle.id, underlying=underlying, trading_date=trading_date,
            session_status="PENDING",
        )
        db.add(row)
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - concurrent create; re-fetch
            db.rollback()
            existing = db.execute(
                select(DailySession).where(
                    DailySession.underlying == underlying, DailySession.trading_date == trading_date,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            raise
        db.refresh(row)
        logger.info("straddle_pulse.session_created underlying=%s date=%s", underlying, trading_date)
        return row
