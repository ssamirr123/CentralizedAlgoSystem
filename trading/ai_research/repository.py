"""
AI Research persistence -- the database is now the sole source of truth
(Section 10: "Do not maintain two independent sources of truth"). No
in-memory cache of any kind; every read goes straight to Postgres/SQLite,
matching this feature's actual load (one poll every ~2s per ACTIVE job,
trivial for a normal Postgres connection).

Plain module-level functions taking/opening a Session, following this
codebase's own existing convention (trading/api/security/audit.py's
audit.record(db, ...), admin_cli.py's direct db.query() calls) rather
than a repository CLASS abstraction this project doesn't otherwise use.

Session lifetime: each function that runs from the long-lived background
research task (service.py's _run_job) opens and closes its OWN short-lived
SessionLocal() -- there is no HTTP request to scope a session to for a
multi-minute background job, the same reason
trading/market_data/service.py's MarketDataService uses its own
`self._session_factory()` calls rather than FastAPI's Depends(get_db).

Nothing here imports trading.common.broker, trading.common.brokers.*, or
trading.algos.* (see tests/ai_research/test_isolation.py's AST scan,
which covers this file too).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from trading.ai_research.jobs import ResearchJob
from trading.ai_research.models import AIResearchRun, AIResearchStage
from trading.ai_research.schemas import (
    NodeStageOut,
    NodeStageStatus,
    ResearchDepth,
    ResearchJobStatus,
    ResearchReportOut,
    ResearchRequestIn,
    ResearchStage,
)
from trading.ai_research.trading_agents_adapter import real_node_order, stage_bucket_for_node
from trading.database.connection import SessionLocal

logger = logging.getLogger("trading.ai_research.repository")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite has no native timezone-aware datetime type -- a DateTime(
    timezone=True) column still round-trips as a naive datetime on
    SQLite (unlike real Postgres, which preserves it). Every value this
    module stores is always UTC, so a naive value read back is assumed
    UTC. Without this, datetime arithmetic between a freshly-created
    _utcnow() and a value just read back from SQLite raises
    "can't subtract offset-naive and offset-aware datetimes"."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_job(row: AIResearchRun) -> ResearchJob:
    stages = tuple(
        NodeStageOut(
            name=s.name, status=NodeStageStatus(s.status), started_at=_aware(s.started_at),
            completed_at=_aware(s.completed_at), duration_ms=s.duration_ms,
        )
        for s in sorted(row.stages, key=lambda s: s.sequence)
    )
    report = ResearchReportOut(**row.report) if row.report else None
    return ResearchJob(
        research_id=row.research_id, symbol=row.symbol, research_date=row.research_date,
        research_depth=ResearchDepth(row.research_depth), status=ResearchJobStatus(row.status),
        created_at=_aware(row.created_at), stage=ResearchStage(row.stage), current_stage=row.current_stage,
        node_stages=stages, started_at=_aware(row.started_at), completed_at=_aware(row.completed_at),
        report=report, error=row.error_message, provider_error=row.error_category,
        # Section 35: a pre-Phase-7 row has NULL here -- interpreted as
        # the exact pre-Phase-7 behavior (US/GENERIC/USD), never guessed
        # at any other value.
        market=row.market or "US", exchange=row.exchange or "GENERIC",
        canonical_id=row.canonical_id, currency=row.currency or "USD",
        market_data_provenance=row.market_data_provenance,
        request=None,  # reconstructed separately by resume via _row_to_request when needed
    )


def _row_to_request(row: AIResearchRun) -> ResearchRequestIn:
    """Rebuild the original request from the persisted row -- used by
    resume_research() after a restart, when no in-memory request object
    survives (Section 15)."""
    from trading.ai_research.schemas import AnalystKey, PortfolioContextIn

    from trading.ai_research.market.instruments import Market

    return ResearchRequestIn(
        symbol=row.symbol, research_date=row.research_date, research_depth=ResearchDepth(row.research_depth),
        market=Market(row.market) if row.market else Market.US,
        selected_analysts=[AnalystKey(a) for a in row.selected_analysts] if row.selected_analysts else None,
        llm_provider=row.llm_provider, deep_think_llm=row.deep_think_llm, quick_think_llm=row.quick_think_llm,
        debate_rounds=row.debate_rounds, risk_debate_rounds=row.risk_debate_rounds, retry_count=row.retry_count,
        data_vendors=row.data_vendors, checkpoint_enabled=row.checkpoint_enabled,
        portfolio=PortfolioContextIn(**row.portfolio) if row.portfolio else None,
    )


def create_run(request: ResearchRequestIn) -> ResearchJob:
    import uuid

    from trading.ai_research.market.instruments import resolve_instrument

    analysts = tuple(a.value for a in request.selected_analysts) if request.selected_analysts else (
        "market", "social", "news", "fundamentals",
    )
    node_names = real_node_order(analysts)
    instrument = resolve_instrument(request.market, request.symbol)

    db = SessionLocal()
    try:
        research_id = str(uuid.uuid4())
        row = AIResearchRun(
            research_id=research_id, symbol=request.symbol, research_date=request.research_date,
            research_depth=request.research_depth.value, status=ResearchJobStatus.QUEUED.value,
            stage=ResearchStage.QUEUED.value,
            market=instrument.market.value, exchange=instrument.exchange.value,
            canonical_id=instrument.canonical_id, currency=instrument.currency,
            llm_provider=request.llm_provider, quick_think_llm=request.quick_think_llm,
            deep_think_llm=request.deep_think_llm,
            selected_analysts=[a.value for a in request.selected_analysts] if request.selected_analysts else None,
            data_vendors=request.data_vendors, debate_rounds=request.debate_rounds,
            risk_debate_rounds=request.risk_debate_rounds, retry_count=request.retry_count,
            checkpoint_enabled=request.checkpoint_enabled,
            portfolio=request.portfolio.model_dump() if request.portfolio else None,
            created_at=_utcnow(),
        )
        db.add(row)
        for i, name in enumerate(node_names):
            db.add(AIResearchStage(research_id=research_id, sequence=i, name=name, status=NodeStageStatus.PENDING.value))
        db.commit()
        db.refresh(row)
        for s in row.stages:
            db.refresh(s)
        return _row_to_job(row)
    finally:
        db.close()


def get_run(research_id: str) -> ResearchJob | None:
    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, research_id)
        return _row_to_job(row) if row else None
    finally:
        db.close()


def get_request_for_resume(research_id: str) -> tuple[ResearchRequestIn, str] | None:
    """Returns (request, status) for a persisted run, or None if unknown --
    used by resume so it works even after a restart with no in-memory
    request object (Section 15)."""
    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, research_id)
        if row is None:
            return None
        return _row_to_request(row), row.status
    finally:
        db.close()


def mark_running(research_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, research_id)
        if row is None:
            return
        now = _utcnow()
        row.status = ResearchJobStatus.RUNNING.value
        row.stage = ResearchStage.ANALYSTS.value
        row.started_at = now
        stages = sorted(row.stages, key=lambda s: s.sequence)
        if stages:
            stages[0].status = NodeStageStatus.RUNNING.value
            stages[0].started_at = now
            row.current_stage = stages[0].name
        db.commit()
    finally:
        db.close()


def mark_node_complete(research_id: str, node_name: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, research_id)
        if row is None or row.status != ResearchJobStatus.RUNNING.value:
            return
        now = _utcnow()
        stages = sorted(row.stages, key=lambda s: s.sequence)

        target = None
        for s in stages:
            if s.name == node_name and s.status in (NodeStageStatus.PENDING.value, NodeStageStatus.RUNNING.value):
                target = s
                break
        if target is None:
            return  # unrecognized/internal node, or already completed -- ignore

        started_at = _aware(target.started_at) or now
        target.status = NodeStageStatus.COMPLETED.value
        target.started_at = started_at
        target.completed_at = now
        target.duration_ms = int((now - started_at).total_seconds() * 1000)

        idx = stages.index(target)
        next_idx = idx + 1
        current_stage = node_name
        if next_idx < len(stages) and stages[next_idx].status == NodeStageStatus.PENDING.value:
            stages[next_idx].status = NodeStageStatus.RUNNING.value
            stages[next_idx].started_at = now
            current_stage = stages[next_idx].name

        row.current_stage = current_stage
        bucket = stage_bucket_for_node(node_name)
        if bucket:
            row.stage = bucket
        db.commit()
    finally:
        db.close()


def mark_completed(research_id: str, report: ResearchReportOut, market_data_provenance: dict | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, research_id)
        if row is None:
            return
        now = _utcnow()
        for s in row.stages:
            if s.status != NodeStageStatus.COMPLETED.value:
                s.status = NodeStageStatus.COMPLETED.value
                s.completed_at = now
        row.status = ResearchJobStatus.COMPLETED.value
        row.stage = ResearchStage.COMPLETED.value
        row.completed_at = now
        row.report = report.model_dump()
        if market_data_provenance is not None:
            row.market_data_provenance = market_data_provenance
        db.commit()
    except Exception:
        # Section 11: a persistence failure must never silently report
        # COMPLETED -- roll back so the run stays in its last-known-good
        # state (RUNNING) rather than a half-written COMPLETED row a
        # caller might read as done.
        db.rollback()
        logger.exception("ai_research.persist_completed_failed research_id=%s", research_id)
        raise
    finally:
        db.close()


def mark_failed(research_id: str, error: str, *, provider_error: str | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, research_id)
        if row is None:
            return
        now = _utcnow()
        for s in row.stages:
            if s.status == NodeStageStatus.RUNNING.value:
                s.status = NodeStageStatus.FAILED.value
                s.completed_at = now
                break
        row.status = ResearchJobStatus.FAILED.value
        row.stage = ResearchStage.FAILED.value
        row.completed_at = now
        row.error_message = error
        row.error_category = provider_error
        db.commit()
    finally:
        db.close()


def list_runs(
    *, page: int = 1, page_size: int = 50, symbol: str | None = None, status: str | None = None,
    date_from: date | None = None, date_to: date | None = None, provider: str | None = None,
) -> tuple[list[AIResearchRun], int]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)  # Section 16: never unbounded

    db = SessionLocal()
    try:
        stmt = select(AIResearchRun)
        count_stmt = select(func.count()).select_from(AIResearchRun)
        if symbol:
            stmt = stmt.where(AIResearchRun.symbol == symbol.upper())
            count_stmt = count_stmt.where(AIResearchRun.symbol == symbol.upper())
        if status:
            stmt = stmt.where(AIResearchRun.status == status)
            count_stmt = count_stmt.where(AIResearchRun.status == status)
        if provider:
            stmt = stmt.where(AIResearchRun.llm_provider == provider)
            count_stmt = count_stmt.where(AIResearchRun.llm_provider == provider)
        if date_from:
            stmt = stmt.where(AIResearchRun.research_date >= date_from)
            count_stmt = count_stmt.where(AIResearchRun.research_date >= date_from)
        if date_to:
            stmt = stmt.where(AIResearchRun.research_date <= date_to)
            count_stmt = count_stmt.where(AIResearchRun.research_date <= date_to)

        total = db.execute(count_stmt).scalar_one()
        stmt = stmt.order_by(AIResearchRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        rows = list(db.execute(stmt).scalars().all())
        # History rows only need scalar columns (research_id, symbol, ...),
        # already loaded -- .stages (a relationship) is intentionally never
        # touched here, so no lazy-load is attempted after db.close().
        return rows, total
    finally:
        db.close()


def recover_interrupted_jobs() -> int:
    """Section 13: on backend startup, any run still QUEUED/RUNNING is
    necessarily stale -- the process that owned it is gone (this is a
    fresh process, its in-memory asyncio tasks don't exist). These are
    marked FAILED with error_category="INTERRUPTED", never silently
    claimed as completed, and never left stuck forever. No new status
    enum value was added (Section 13's own "otherwise mark FAILED"
    fallback) -- this keeps the API/frontend status model exactly as
    Phase 4 shipped it. Returns the number of rows recovered."""
    db = SessionLocal()
    try:
        stale = db.execute(
            select(AIResearchRun).where(
                AIResearchRun.status.in_([ResearchJobStatus.QUEUED.value, ResearchJobStatus.RUNNING.value])
            )
        ).scalars().all()
        now = _utcnow()
        for row in stale:
            was_running = row.status == ResearchJobStatus.RUNNING.value
            for s in row.stages:
                if s.status == NodeStageStatus.RUNNING.value:
                    s.status = NodeStageStatus.FAILED.value
                    s.completed_at = now
            row.status = ResearchJobStatus.FAILED.value
            row.stage = ResearchStage.FAILED.value
            row.completed_at = now
            row.error_category = "INTERRUPTED"
            row.error_message = (
                "Research was interrupted by a backend restart while "
                f"{'RUNNING' if was_running else 'QUEUED'}."
            )
        db.commit()
        if stale:
            logger.warning("ai_research.recovered_interrupted_jobs count=%d", len(stale))
        return len(stale)
    finally:
        db.close()


def purge_older_than(retention_days: int) -> int:
    """Section 22: explicit, opt-in retention cleanup -- never runs unless
    a caller (e.g. a deliberately-scheduled maintenance task) actually
    invokes this with a positive retention_days. Nothing in this phase
    calls it automatically."""
    if retention_days <= 0:
        return 0
    cutoff = _utcnow() - timedelta(days=retention_days)
    db = SessionLocal()
    try:
        rows = db.execute(select(AIResearchRun).where(AIResearchRun.created_at < cutoff)).scalars().all()
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)
    finally:
        db.close()
