"""
Straddle Pulse -- expiry-cycle identity.

Rule: the cycle boundary is the actual listed expiry date for the
underlying, never a calendar week / weekday / ISO week number. Cycle
start is the day after the *previous* listed expiry (best-effort -- if no
earlier expiry is known yet, the start date is left equal to the expiry
date and firms up as daily sessions are created going forward).

Generic over ``underlying`` -- callers pass the underlying's actual listed
expiries (from ``InstrumentMaster.list_expiries``); no NIFTY/SENSEX
branching lives here.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.market_data.models import ExpiryCycle
from trading.market_data.underlying_config import underlying_config

logger = logging.getLogger("trading.market_data.expiry_cycle")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExpiryCycleService:
    def get_or_create_active_cycle(
        self, db: Session, underlying: str, expiries: list[date], today: date,
    ) -> ExpiryCycle | None:
        underlying = underlying.strip().upper()
        upcoming = sorted(e for e in expiries if e >= today)
        if not upcoming:
            return None
        expiry_date = upcoming[0]

        existing = db.execute(
            select(ExpiryCycle).where(
                ExpiryCycle.underlying == underlying, ExpiryCycle.expiry_date == expiry_date,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        earlier = sorted(e for e in expiries if e < expiry_date)
        cycle_start_date = (earlier[-1] + timedelta(days=1)) if earlier else expiry_date

        cfg = underlying_config(underlying)
        row = ExpiryCycle(
            underlying=underlying,
            exchange=cfg.option_exchange.value,
            expiry_date=expiry_date,
            cycle_start_date=cycle_start_date,
            cycle_end_date=expiry_date,
            status="ACTIVE",
        )
        db.add(row)
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - concurrent create; re-fetch
            db.rollback()
            existing = db.execute(
                select(ExpiryCycle).where(
                    ExpiryCycle.underlying == underlying, ExpiryCycle.expiry_date == expiry_date,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            raise
        db.refresh(row)
        logger.info("straddle_pulse.cycle_created underlying=%s expiry=%s", underlying, expiry_date)
        return row

    def complete_past_cycles(self, db: Session, underlying: str, today: date) -> int:
        underlying = underlying.strip().upper()
        rows = db.execute(
            select(ExpiryCycle).where(
                ExpiryCycle.underlying == underlying,
                ExpiryCycle.status == "ACTIVE",
                ExpiryCycle.cycle_end_date < today,
            )
        ).scalars().all()
        for row in rows:
            row.status = "COMPLETED"
            row.completed_at = _utcnow()
        if rows:
            db.commit()
        return len(rows)
