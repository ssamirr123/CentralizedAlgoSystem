"""
Server-side stale / missing heartbeat detection.

Replaces the legacy watcher that scanned the strategy_heartbeats table.
This version reads the canonical control-center model (algos + heartbeats
+ servers) -- it does NOT introduce a second heartbeat system, it only
observes the one that already exists.

For every algo that is not deliberately STOPPED, it compares the most
recent control-center heartbeat (MAX(heartbeats.timestamp), falling back
to algos.updated_at when the algo has never sent one) against
STALE_THRESHOLD_MINUTES. If it is older than that, it emits the existing
alert_service.heartbeat_missing(...) alert -- same alert method, same
signature, same dedup behaviour as before.

STRICTLY read-only + alert-only. It never stops a process, cancels an
order, squares off a position, or writes to the database. The only side
effect is the alert call (and a WARNING log line).

Environment variables (unchanged):
    STALE_THRESHOLD_MINUTES        default 2
    STALE_CHECK_INTERVAL_SECONDS   default 60
    DISABLE_BACKGROUND_WATCHER     handled by the caller (the factory lifespan)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from alerts.telegram import alert_service
from trading.database import models
from trading.database.connection import SessionLocal

STALE_THRESHOLD_MINUTES = float(os.environ.get("STALE_THRESHOLD_MINUTES", "2"))
STALE_CHECK_INTERVAL_SECONDS = int(os.environ.get("STALE_CHECK_INTERVAL_SECONDS", "60"))

logger = logging.getLogger("strategy_monitor")


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def check_stale_heartbeats_once(
    db: Session, *, now: datetime | None = None
) -> list[tuple[str, str, float]]:
    """Run one scan pass over the canonical model.

    Fires alert_service.heartbeat_missing(algo_name, server_name,
    minutes=...) for every non-STOPPED algo whose latest heartbeat is
    older than STALE_THRESHOLD_MINUTES. Returns the list of
    (algo_name, server_name, minutes_silent) it alerted on. Read-only.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_THRESHOLD_MINUTES)

    last_hb = (
        db.query(
            models.Heartbeat.algo_id,
            func.max(models.Heartbeat.timestamp).label("last_ts"),
        )
        .group_by(models.Heartbeat.algo_id)
        .subquery()
    )
    rows = (
        db.query(models.Algo, models.Server.name, last_hb.c.last_ts)
        .join(models.Server, models.Algo.server_id == models.Server.id)
        .outerjoin(last_hb, last_hb.c.algo_id == models.Algo.id)
        .filter(models.Algo.status != "STOPPED")
        .all()
    )

    alerted: list[tuple[str, str, float]] = []
    for algo, server_name, last_ts in rows:
        reference = _as_utc(last_ts) or _as_utc(algo.updated_at)
        if reference is None or reference >= cutoff:
            continue
        silent_for = (now - reference).total_seconds() / 60
        logger.warning(
            "Stale heartbeat detected | algo=%s | server=%s | status=%s | silent=%.1f min",
            algo.name, server_name, algo.status, silent_for,
        )
        alert_service.heartbeat_missing(algo.name, server_name, minutes=silent_for)
        alerted.append((algo.name, server_name, silent_for))
    return alerted


async def stale_heartbeat_watcher() -> None:
    """Background loop: one check_stale_heartbeats_once() pass every
    STALE_CHECK_INTERVAL_SECONDS. Best-effort -- any error is logged and
    the loop continues."""
    logger.info("Stale heartbeat watcher started (interval=%ds)", STALE_CHECK_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(STALE_CHECK_INTERVAL_SECONDS)
        try:
            db = SessionLocal()
            try:
                check_stale_heartbeats_once(db)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error in stale heartbeat watcher: %s", exc)
