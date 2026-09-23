"""
AI Research Backtesting persistence -- same conventions as
trading/ai_research/repository.py (Phase 5): plain module-level functions,
each opening/closing its own SessionLocal(), the database as sole source
of truth. Reuses that module's `_aware()` UTC-normalization helper rather
than duplicating it (SQLite round-trips a DateTime(timezone=True) column
as naive -- see that function's own docstring).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from trading.ai_research.backtest.models import AIBacktestJob, AIBacktestRun
from trading.ai_research.backtest.schemas import BacktestRequestIn
from trading.database.connection import SessionLocal

logger = logging.getLogger("trading.ai_research.backtest.repository")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_backtest(request: BacktestRequestIn, dates: tuple[str, ...], *, holding_days: int) -> str:
    """Creates the job row plus one PENDING cell row per (symbol, date).
    Returns the new backtest_id."""
    backtest_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        job = AIBacktestJob(
            backtest_id=backtest_id,
            symbols=list(request.symbols),
            start_date=request.start_date,
            end_date=request.end_date,
            frequency=request.frequency.value,
            market=request.market.value,
            selected_analysts=[a.value for a in request.selected_analysts] if request.selected_analysts else None,
            research_depth=request.research_depth.value,
            llm_provider=request.llm_provider,
            quick_think_llm=request.quick_think_llm,
            deep_think_llm=request.deep_think_llm,
            debate_rounds=request.debate_rounds,
            risk_debate_rounds=request.risk_debate_rounds,
            retry_count=request.retry_count,
            data_vendors=request.data_vendors,
            holding_days=holding_days,
            status="QUEUED",
            total_runs=len(request.symbols) * len(dates),
            created_at=_utcnow(),
        )
        db.add(job)
        for symbol in request.symbols:
            for d in dates:
                db.add(AIBacktestRun(
                    backtest_id=backtest_id, symbol=symbol,
                    cell_date=datetime.strptime(d, "%Y-%m-%d").date(),
                    status="PENDING",
                ))
        db.commit()
        return backtest_id
    finally:
        db.close()


def get_backtest(backtest_id: str) -> AIBacktestJob | None:
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        if job is not None:
            len(job.cells)  # force the lazy relationship to load before the session closes
        return job
    finally:
        db.close()


def get_cells(backtest_id: str) -> list[AIBacktestRun]:
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        return list(job.cells) if job else []
    finally:
        db.close()


def list_cells_page(
    backtest_id: str, *, page: int = 1, page_size: int = 50, symbol: str | None = None,
) -> tuple[list[AIBacktestRun], int]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    db = SessionLocal()
    try:
        stmt = select(AIBacktestRun).where(AIBacktestRun.backtest_id == backtest_id)
        count_stmt = select(func.count()).select_from(AIBacktestRun).where(AIBacktestRun.backtest_id == backtest_id)
        if symbol:
            stmt = stmt.where(AIBacktestRun.symbol == symbol.upper())
            count_stmt = count_stmt.where(AIBacktestRun.symbol == symbol.upper())
        total = db.execute(count_stmt).scalar_one()
        stmt = stmt.order_by(AIBacktestRun.symbol, AIBacktestRun.cell_date).offset((page - 1) * page_size).limit(page_size)
        rows = list(db.execute(stmt).scalars().all())
        return rows, total
    finally:
        db.close()


def get_not_completed_cells(backtest_id: str) -> list[tuple[str, str]]:
    """(symbol, cell_date-iso) pairs that are NOT COMPLETED -- what
    resume_backtest() will (re)run (Section 20: never rerun an
    already-COMPLETED cell; a PENDING/RUNNING/FAILED cell is fair game,
    since it never produced a usable decision)."""
    db = SessionLocal()
    try:
        rows = db.execute(
            select(AIBacktestRun).where(
                AIBacktestRun.backtest_id == backtest_id,
                AIBacktestRun.status != "COMPLETED",
            )
        ).scalars().all()
        return [(r.symbol, r.cell_date.isoformat()) for r in rows]
    finally:
        db.close()


def clear_cancel_flag(backtest_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        if job is None:
            return
        job.cancel_requested = False
        db.commit()
    finally:
        db.close()


def mark_backtest_running(backtest_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        if job is None:
            return
        job.status = "RUNNING"
        if job.started_at is None:
            job.started_at = _utcnow()
        db.commit()
    finally:
        db.close()


def mark_cell_running(backtest_id: str, symbol: str, cell_date: str) -> None:
    db = SessionLocal()
    try:
        cell = _find_cell(db, backtest_id, symbol, cell_date)
        if cell is None:
            return
        cell.status = "RUNNING"
        cell.started_at = _utcnow()
        job = db.get(AIBacktestJob, backtest_id)
        if job is not None:
            job.current_symbol = symbol
            job.current_date = cell.cell_date
        db.commit()
    finally:
        db.close()


def mark_cell_completed(
    backtest_id: str, symbol: str, cell_date: str, *, research_id: str,
    raw_decision: str | None, normalized_decision: str | None,
) -> None:
    db = SessionLocal()
    try:
        cell = _find_cell(db, backtest_id, symbol, cell_date)
        if cell is None:
            return
        cell.status = "COMPLETED"
        cell.research_id = research_id
        cell.raw_decision = (raw_decision or "")[:2000] or None
        cell.normalized_decision = normalized_decision
        cell.evaluation_status = "PENDING"
        cell.completed_at = _utcnow()
        job = db.get(AIBacktestJob, backtest_id)
        if job is not None:
            job.completed_runs += 1
        db.commit()
    finally:
        db.close()


def mark_cell_failed(backtest_id: str, symbol: str, cell_date: str, error: str) -> None:
    db = SessionLocal()
    try:
        cell = _find_cell(db, backtest_id, symbol, cell_date)
        if cell is None:
            return
        cell.status = "FAILED"
        cell.error_message = (error or "")[:1000]
        cell.completed_at = _utcnow()
        job = db.get(AIBacktestJob, backtest_id)
        if job is not None:
            job.failed_runs += 1
        db.commit()
    finally:
        db.close()


def mark_cell_evaluated(
    backtest_id: str, symbol: str, cell_date: str, *, raw_return: float, alpha_return: float,
    benchmark: str, holding_days: int, resolution_date: str,
) -> None:
    db = SessionLocal()
    try:
        cell = _find_cell(db, backtest_id, symbol, cell_date)
        if cell is None:
            return
        cell.evaluation_status = "RESOLVED"
        cell.raw_return = raw_return
        cell.alpha_return = alpha_return
        cell.benchmark = benchmark
        cell.holding_days = holding_days
        cell.resolution_date = datetime.strptime(resolution_date, "%Y-%m-%d").date()
        db.commit()
    finally:
        db.close()


def mark_backtest_completed(backtest_id: str, metrics: dict) -> None:
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        if job is None:
            return
        job.status = "COMPLETED"
        job.metrics = metrics
        job.completed_at = _utcnow()
        job.current_symbol = None
        job.current_date = None
        db.commit()
    finally:
        db.close()


def mark_backtest_failed(backtest_id: str, error: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        if job is None:
            return
        job.status = "FAILED"
        job.error_message = (error or "")[:1000]
        job.completed_at = _utcnow()
        db.commit()
    finally:
        db.close()


def mark_backtest_cancelled(backtest_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        if job is None:
            return
        job.status = "CANCELLED"
        job.completed_at = _utcnow()
        job.current_symbol = None
        job.current_date = None
        db.commit()
    finally:
        db.close()


def request_cancel(backtest_id: str) -> bool:
    """Sets the cooperative cancel flag the running loop polls between
    cells (Section 22) -- never kills mid-flight work. Returns False if
    the backtest is not in a cancellable (QUEUED/RUNNING) state."""
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        if job is None or job.status not in ("QUEUED", "RUNNING"):
            return False
        job.cancel_requested = True
        db.commit()
        return True
    finally:
        db.close()


def is_cancel_requested(backtest_id: str) -> bool:
    db = SessionLocal()
    try:
        job = db.get(AIBacktestJob, backtest_id)
        return bool(job and job.cancel_requested)
    finally:
        db.close()


def list_backtests(
    *, page: int = 1, page_size: int = 50, status: str | None = None,
) -> tuple[list[AIBacktestJob], int]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    db = SessionLocal()
    try:
        stmt = select(AIBacktestJob)
        count_stmt = select(func.count()).select_from(AIBacktestJob)
        if status:
            stmt = stmt.where(AIBacktestJob.status == status)
            count_stmt = count_stmt.where(AIBacktestJob.status == status)
        total = db.execute(count_stmt).scalar_one()
        stmt = stmt.order_by(AIBacktestJob.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        rows = list(db.execute(stmt).scalars().all())
        return rows, total
    finally:
        db.close()


def recover_interrupted_backtests() -> int:
    """Section 19: on backend startup, any backtest job still QUEUED/RUNNING
    is stale -- its owning process is gone. Marked FAILED honestly (never
    claimed COMPLETED); any cell still RUNNING is marked FAILED too.
    Already-COMPLETED cells are untouched, so a later resume_backtest()
    only repeats the genuinely unfinished ones. Returns the number of jobs
    recovered."""
    db = SessionLocal()
    try:
        stale = db.execute(
            select(AIBacktestJob).where(AIBacktestJob.status.in_(["QUEUED", "RUNNING"]))
        ).scalars().all()
        now = _utcnow()
        for job in stale:
            for cell in job.cells:
                if cell.status == "RUNNING":
                    cell.status = "FAILED"
                    cell.error_message = "Interrupted by a backend restart while RUNNING."
                    cell.completed_at = now
                    job.failed_runs += 1
            job.status = "FAILED"
            job.error_message = "Backtest was interrupted by a backend restart. Resume to continue pending cells."
            job.completed_at = now
            job.current_symbol = None
            job.current_date = None
        db.commit()
        if stale:
            logger.warning("ai_research.backtest.recovered_interrupted count=%d", len(stale))
        return len(stale)
    finally:
        db.close()


def purge_older_than(retention_days: int) -> int:
    """Opt-in only (never auto-invoked), matching ai_research.repository's
    own purge_older_than."""
    if retention_days <= 0:
        return 0
    cutoff = _utcnow() - timedelta(days=retention_days)
    db = SessionLocal()
    try:
        rows = db.execute(select(AIBacktestJob).where(AIBacktestJob.created_at < cutoff)).scalars().all()
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)
    finally:
        db.close()


def _find_cell(db, backtest_id: str, symbol: str, cell_date: str) -> AIBacktestRun | None:
    d = datetime.strptime(cell_date, "%Y-%m-%d").date()
    return db.execute(
        select(AIBacktestRun).where(
            AIBacktestRun.backtest_id == backtest_id,
            AIBacktestRun.symbol == symbol,
            AIBacktestRun.cell_date == d,
        )
    ).scalar_one_or_none()
