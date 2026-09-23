"""Phase 10 -- durable persistence, mirroring trading/ai_research/repository.py's pattern."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from trading.ai_options_research.jobs import NodeStageOut, OptionsResearchJob
from trading.ai_options_research.models import STAGE_NAMES, AIOptionsResearchRun, AIOptionsResearchStage
from trading.ai_options_research.schemas import OptionsResearchRequestIn
from trading.database.connection import SessionLocal


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_job(row: AIOptionsResearchRun) -> OptionsResearchJob:
    stages = tuple(
        NodeStageOut(name=s.name, status=s.status, started_at=s.started_at, completed_at=s.completed_at)
        for s in sorted(row.stages, key=lambda s: s.sequence)
    )
    error_category = row.error_category
    return OptionsResearchJob(
        research_id=row.research_id, underlying=row.underlying, expiry=row.expiry, as_of=row.as_of,
        status=row.status, current_stage=row.current_stage,
        research_snapshot_id=row.research_snapshot_id, evidence_quality=row.evidence_quality,
        report=row.report, error=row.error_message, provider_error=error_category,
        created_at=row.created_at, started_at=row.started_at, completed_at=row.completed_at,
        node_stages=stages,
    )


def create_run(request: OptionsResearchRequestIn) -> OptionsResearchJob:
    research_id = str(uuid.uuid4())
    with SessionLocal() as db:
        row = AIOptionsResearchRun(
            research_id=research_id, underlying=request.underlying.upper(), status="QUEUED",
            llm_provider=request.llm_provider, quick_think_llm=request.quick_think_llm,
            deep_think_llm=request.deep_think_llm, debate_rounds=request.debate_rounds,
            candidate_limit=request.candidate_limit,
            strategy_universe=[s.value for s in request.resolved_strategy_universe()],
            generation_config={
                "strike_window": request.strike_window, "min_delta": request.min_delta,
                "max_delta": request.max_delta, "minimum_volume": request.minimum_volume,
                "minimum_oi": request.minimum_oi, "maximum_bid_ask_spread": request.maximum_bid_ask_spread,
                "wing_width": request.wing_width, "hedge_wing_width": request.hedge_wing_width,
                "expiry": request.expiry,
            },
            created_at=_now(),
        )
        db.add(row)
        db.flush()
        for i, name in enumerate(STAGE_NAMES):
            db.add(AIOptionsResearchStage(research_id=research_id, sequence=i, name=name, status="PENDING"))
        db.commit()
        db.refresh(row)
        _ = row.stages
        return _row_to_job(row)


def get_run(research_id: str) -> OptionsResearchJob | None:
    with SessionLocal() as db:
        row = db.get(AIOptionsResearchRun, research_id)
        return _row_to_job(row) if row else None


def mark_running(research_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(AIOptionsResearchRun, research_id)
        if row is None:
            return
        row.status = "RUNNING"
        row.started_at = _now()
        first = next((s for s in sorted(row.stages, key=lambda s: s.sequence)), None)
        if first is not None:
            first.status = "RUNNING"
            first.started_at = _now()
            row.current_stage = first.name
        db.commit()


def mark_stage_complete(research_id: str, stage_name: str) -> None:
    with SessionLocal() as db:
        stages = db.execute(
            select(AIOptionsResearchStage)
            .where(AIOptionsResearchStage.research_id == research_id)
            .order_by(AIOptionsResearchStage.sequence)
        ).scalars().all()
        target = next((s for s in stages if s.name == stage_name and s.status in ("PENDING", "RUNNING")), None)
        if target is None:
            return
        target.status = "COMPLETED"
        target.completed_at = _now()
        nxt = next((s for s in stages if s.sequence == target.sequence + 1), None)
        row = db.get(AIOptionsResearchRun, research_id)
        if nxt is not None:
            nxt.status = "RUNNING"
            nxt.started_at = _now()
            if row is not None:
                row.current_stage = nxt.name
        db.commit()


def mark_completed(research_id: str, report: dict, *, research_snapshot_id: str | None, evidence_quality: str | None) -> None:
    with SessionLocal() as db:
        row = db.get(AIOptionsResearchRun, research_id)
        if row is None:
            return
        row.status = "COMPLETED"
        row.report = report
        row.research_snapshot_id = research_snapshot_id
        row.evidence_quality = evidence_quality
        row.completed_at = _now()
        row.current_stage = None
        db.commit()


def mark_failed(
    research_id: str, error_message: str, *, error_category: str | None = None,
    research_snapshot_id: str | None = None, evidence_quality: str | None = None,
) -> None:
    """Section 54: even a failed run records which Phase 9 snapshot it
    was analyzing, when one was successfully frozen before the failure
    (e.g. an LLM error after the chain was already fetched) -- never
    left blank just because the run didn't reach completion."""
    with SessionLocal() as db:
        row = db.get(AIOptionsResearchRun, research_id)
        if row is None:
            return
        row.status = "FAILED"
        row.error_message = error_message[:2000]
        row.error_category = error_category
        if research_snapshot_id is not None:
            row.research_snapshot_id = research_snapshot_id
        if evidence_quality is not None:
            row.evidence_quality = evidence_quality
        row.completed_at = _now()
        db.commit()


def list_runs(page: int, page_size: int, *, underlying: str | None = None, status: str | None = None) -> tuple[list[OptionsResearchJob], int]:
    with SessionLocal() as db:
        stmt = select(AIOptionsResearchRun)
        if underlying:
            stmt = stmt.where(AIOptionsResearchRun.underlying == underlying.upper())
        if status:
            stmt = stmt.where(AIOptionsResearchRun.status == status)
        total = len(db.execute(stmt).scalars().all())
        rows = db.execute(
            stmt.order_by(AIOptionsResearchRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars().all()
        return [_row_to_job(r) for r in rows], total


def mark_remaining_stages_skipped(research_id: str) -> None:
    """Section 6: when the data-quality gate short-circuits a run to an
    explicit degraded/insufficient-data result, the remaining agent
    stages never execute -- reflected honestly as SKIPPED, never left
    looking like an unfinished/stuck PENDING run."""
    with SessionLocal() as db:
        stages = db.execute(
            select(AIOptionsResearchStage).where(AIOptionsResearchStage.research_id == research_id)
        ).scalars().all()
        for s in stages:
            if s.status in ("PENDING", "RUNNING"):
                s.status = "SKIPPED"
                s.completed_at = _now()
        row = db.get(AIOptionsResearchRun, research_id)
        if row is not None:
            row.current_stage = None
        db.commit()


def recover_interrupted_jobs() -> int:
    """Same restart-durability rule as trading/ai_research/repository.py:
    a process crash mid-run must never leave a job stuck at RUNNING
    forever -- mark it FAILED so it is visibly resumable/re-runnable."""
    with SessionLocal() as db:
        rows = db.execute(select(AIOptionsResearchRun).where(AIOptionsResearchRun.status == "RUNNING")).scalars().all()
        for row in rows:
            row.status = "FAILED"
            row.error_message = "Interrupted by backend restart"
            row.error_category = "INTERRUPTED"
            row.completed_at = _now()
        db.commit()
        return len(rows)
