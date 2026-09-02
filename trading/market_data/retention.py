"""
Phase 10 -- retention.

    MARKET_DATA_RETENTION_DAYS   default 365  (index 1-minute candles)
    OPTION_DATA_RETENTION_DAYS   default 180  (option 1-minute candles)

Nothing here runs automatically. ``run_retention`` is invoked explicitly
(a scheduled admin action / cron), never during development. It deletes
only candle rows older than the cutoff; ``option_contracts`` metadata is
never touched.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from trading.core.config import load_settings
from trading.database import models

logger = logging.getLogger("trading.market_data.retention")


def purge_market_candles(db: Session, *, before: datetime) -> int:
    result = db.execute(delete(models.MarketCandle).where(models.MarketCandle.timestamp < before))
    db.commit()
    return int(result.rowcount or 0)


def purge_option_candles(db: Session, *, before: datetime) -> int:
    result = db.execute(delete(models.OptionCandle).where(models.OptionCandle.timestamp < before))
    db.commit()
    return int(result.rowcount or 0)


def run_retention(
    db: Session,
    *,
    index_days: int | None = None,
    option_days: int | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    settings = load_settings()
    index_days = settings.market_data_retention_days if index_days is None else index_days
    option_days = settings.option_data_retention_days if option_days is None else option_days
    now = now or datetime.now(timezone.utc)

    idx_before = now - timedelta(days=index_days)
    opt_before = now - timedelta(days=option_days)

    if dry_run:
        from sqlalchemy import func, select

        idx_n = db.execute(
            select(func.count()).select_from(models.MarketCandle).where(models.MarketCandle.timestamp < idx_before)
        ).scalar_one()
        opt_n = db.execute(
            select(func.count()).select_from(models.OptionCandle).where(models.OptionCandle.timestamp < opt_before)
        ).scalar_one()
        return {"dry_run": True, "index_candles": idx_n, "option_candles": opt_n,
                "index_before": idx_before.isoformat(), "option_before": opt_before.isoformat()}

    idx_n = purge_market_candles(db, before=idx_before)
    opt_n = purge_option_candles(db, before=opt_before)
    logger.info("market_data.retention purged index=%d option=%d", idx_n, opt_n)
    return {"dry_run": False, "index_candles": idx_n, "option_candles": opt_n,
            "index_before": idx_before.isoformat(), "option_before": opt_before.isoformat()}
