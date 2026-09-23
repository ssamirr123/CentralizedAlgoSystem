"""
AI Research Engine API -- read-only, research-only routes.

    POST /api/ai-research                              submit a research job (returns immediately)
    GET  /api/ai-research/{research_id}                 full result (report once COMPLETED)
    GET  /api/ai-research/{research_id}/status          lightweight lifecycle/stage poll
    GET  /api/ai-research/{research_id}/stages          REAL per-node graph progress (Phase 3)
    POST /api/ai-research/{research_id}/resume          re-submit a FAILED job with identical params
    GET  /api/ai-research/history                       recent jobs known to this backend instance
    GET  /api/ai-research/config/options                discoverable config choices
    GET  /api/ai-research/memory                         decision-memory entries (read-only)
    GET  /api/ai-research/memory/{memory_id}             one decision-memory entry

Every route checks the AI_RESEARCH_ENABLED feature flag FIRST and returns
ResearchDisabledOut (HTTP 200, not an error) when it's off -- the default.
No route ever instantiates TradingAgentsGraph or the adapter directly;
everything goes through AIResearchService (Section 9). No route accepts
or forwards anything broker/order/strategy-shaped -- ResearchRequestIn's
`extra="forbid"` rejects such fields at the FastAPI validation layer,
before this module's code ever runs.

Section 11 (real-time transport assessment): polling this /stages
endpoint every 1-2s is recommended over SSE/WebSocket for the future
React UI. A single research run is a single user's one-off action (not a
shared, high-frequency data feed like the existing market-data WebSocket
at /api/ws), AI_RESEARCH_MAX_CONCURRENT already bounds how many run at
once, and stage transitions land at most a few times per minute (each
node is a multi-second LLM call) -- there is no throughput/latency
pressure a 1-2s poll can't comfortably meet. Adding a second real-time
transport would duplicate infrastructure the app already has (the
realtime WebSocket) for a workload that doesn't need it. Revisit only if
a future phase needs sub-second updates or server-push across many
concurrent viewers of the SAME research_id.
"""
from __future__ import annotations

from datetime import date as date_

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from trading.ai_research import service
from trading.ai_research.config import load_ai_research_settings
from trading.ai_research.jobs import ResearchJob
from trading.ai_research.market.instruments import InvalidSymbolError
from trading.ai_research.schemas import (
    MemoryEntryOut,
    NodeStageOut,
    ResearchConfigOptionsOut,
    ResearchDisabledOut,
    ResearchHistoryEntryOut,
    ResearchHistoryPageOut,
    ResearchJobStatus,
    ResearchRequestIn,
    ResearchResultOut,
    ResearchStatusOut,
    ResearchSubmittedOut,
    ResumeRequestedOut,
    StagesOut,
)
from trading.ai_research.service import ResumeNotAllowedError
from trading.api.deps import enforce_rate_limit, require_permission
from trading.api.security.permissions import Permission

router = APIRouter(
    prefix="/ai-research", tags=["ai-research"], dependencies=[Depends(enforce_rate_limit)],
)

_VIEW = require_permission(Permission.VIEW)


def _job_to_status(job: ResearchJob) -> ResearchStatusOut:
    return ResearchStatusOut(
        research_id=job.research_id, symbol=job.symbol, research_date=job.research_date,
        research_depth=job.research_depth, status=job.status, stage=job.stage,
        current_stage=job.current_stage, created_at=job.created_at, started_at=job.started_at,
        completed_at=job.completed_at, error=job.error, provider_error=job.provider_error,
        market=job.market, exchange=job.exchange, currency=job.currency,
        market_data_provenance=job.market_data_provenance,
    )


def _job_to_result(job: ResearchJob) -> ResearchResultOut:
    return ResearchResultOut(
        research_id=job.research_id, symbol=job.symbol, research_date=job.research_date,
        research_depth=job.research_depth, status=job.status, stage=job.stage,
        created_at=job.created_at, started_at=job.started_at, completed_at=job.completed_at,
        report=job.report, error=job.error, provider_error=job.provider_error,
        market=job.market, exchange=job.exchange, currency=job.currency,
        market_data_provenance=job.market_data_provenance,
    )


@router.post("", response_model=ResearchSubmittedOut | ResearchDisabledOut)
async def submit_research(
    body: ResearchRequestIn, request: Request, principal=Depends(_VIEW),
) -> ResearchSubmittedOut | ResearchDisabledOut:
    # MUST be async (not run_in_threadpool'd like the read routes below):
    # service.submit_research() calls asyncio.create_task() internally,
    # which requires a running event loop on the CURRENT thread. A sync
    # `def` handler runs in FastAPI's worker threadpool instead, which has
    # no event loop of its own -- confirmed by a real Phase 2 test
    # failure (RuntimeError: no running event loop) before this fix.
    if not service.is_enabled():
        return ResearchDisabledOut()
    try:
        job = service.submit_research(body)
    except InvalidSymbolError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    except service.NonTradingDateError as exc:
        # Section 19: structured, useful feedback -- never a silently
        # substituted date.
        v = exc.validation
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {
            "message": v.reason,
            "previous_trading_day": v.previous_trading_day.isoformat() if v.previous_trading_day else None,
            "next_trading_day": v.next_trading_day.isoformat() if v.next_trading_day else None,
        }) from None
    return ResearchSubmittedOut(research_id=job.research_id, status=job.status, created_at=job.created_at)


@router.get("/history", response_model=ResearchHistoryPageOut | ResearchDisabledOut)
def get_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    symbol: str | None = None,
    status_filter: ResearchJobStatus | None = Query(None, alias="status"),
    date_from: date_ | None = None,
    date_to: date_ | None = None,
    provider: str | None = None,
    principal=Depends(_VIEW),
) -> ResearchHistoryPageOut | ResearchDisabledOut:
    """Section 16: database-backed, paginated, filterable -- replaces
    Phase 2-4's unbounded in-memory list. page_size is capped at 200
    (never unbounded, per Section 16's own instruction)."""
    if not service.is_enabled():
        return ResearchDisabledOut()
    rows, total = service.list_history(
        page=page, page_size=page_size, symbol=symbol,
        status=status_filter.value if status_filter else None,
        date_from=date_from, date_to=date_to, provider=provider,
    )
    from trading.ai_research.repository import _aware

    items = [
        ResearchHistoryEntryOut(
            research_id=r.research_id, symbol=r.symbol, research_date=r.research_date,
            research_depth=r.research_depth, status=r.status, llm_provider=r.llm_provider,
            created_at=_aware(r.created_at), completed_at=_aware(r.completed_at),
            market=r.market or "US", currency=r.currency or "USD",
        )
        for r in rows
    ]
    return ResearchHistoryPageOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/config/options", response_model=ResearchConfigOptionsOut)
def get_config_options(principal=Depends(_VIEW)) -> ResearchConfigOptionsOut:
    settings = load_ai_research_settings()
    return ResearchConfigOptionsOut(
        enabled=settings.enabled, max_concurrent=settings.max_concurrent,
        timeout_seconds=settings.timeout_seconds,
    )


@router.get("/memory", response_model=list[MemoryEntryOut] | ResearchDisabledOut)
def get_memory_list(limit: int = 50, principal=Depends(_VIEW)) -> list[MemoryEntryOut] | ResearchDisabledOut:
    """Read-only decision-memory browsing (Section 14). Only ever reads
    the one log file this adapter itself writes to, under our own
    controlled AIResearchSettings.data_dir -- no client-supplied path of
    any kind is involved, so there is nothing for a path-traversal
    attempt to reach."""
    if not service.is_enabled():
        return ResearchDisabledOut()
    entries = service.list_memory(limit=min(max(limit, 1), 200))
    return [
        MemoryEntryOut(
            memory_id=e.memory_id, ticker=e.ticker, trade_date=e.trade_date, rating=e.rating,
            pending=e.pending, decision=e.decision, reflection=e.reflection,
        )
        for e in entries
    ]


@router.get("/memory/{memory_id}", response_model=MemoryEntryOut | ResearchDisabledOut)
def get_memory_one(memory_id: str, principal=Depends(_VIEW)) -> MemoryEntryOut | ResearchDisabledOut:
    """`memory_id` is a purely logical `trade_date:ticker` key produced by
    list_memory_entries() -- it is looked up against already-parsed
    entries, never used to build a filesystem path, so a value like
    `../../etc/passwd` simply matches no entry (404), it cannot traverse
    anywhere."""
    if not service.is_enabled():
        return ResearchDisabledOut()
    entry = service.get_memory(memory_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown memory_id: {memory_id!r}")
    return MemoryEntryOut(
        memory_id=entry.memory_id, ticker=entry.ticker, trade_date=entry.trade_date,
        rating=entry.rating, pending=entry.pending, decision=entry.decision, reflection=entry.reflection,
    )


@router.get("/{research_id}", response_model=ResearchResultOut | ResearchDisabledOut)
def get_research(research_id: str, principal=Depends(_VIEW)) -> ResearchResultOut | ResearchDisabledOut:
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_job(research_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown research_id: {research_id!r}")
    return _job_to_result(job)


@router.get("/{research_id}/status", response_model=ResearchStatusOut | ResearchDisabledOut)
def get_research_status(research_id: str, principal=Depends(_VIEW)) -> ResearchStatusOut | ResearchDisabledOut:
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_job(research_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown research_id: {research_id!r}")
    return _job_to_status(job)


@router.get("/{research_id}/stages", response_model=StagesOut | ResearchDisabledOut)
def get_research_stages(research_id: str, principal=Depends(_VIEW)) -> StagesOut | ResearchDisabledOut:
    """Phase 3: REAL per-node graph progress, replacing Phase 2's
    ineffective callback-based placeholder entirely (no dead code left
    behind -- the old _stage_for_node-only /stages implementation and its
    coarse-only tracking are gone). Each entry's `name` is an ACTUAL
    TradingAgents graph node name; status reflects genuinely observed
    LangGraph "updates" events (see trading_agents_adapter.py's module
    docstring for the completion-vs-start nuance). Analysts NOT in the
    job's selected_analysts are simply absent from the list (Section 7's
    "omit them" option) rather than shown as perpetually PENDING."""
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_job(research_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown research_id: {research_id!r}")

    stages = [
        NodeStageOut(
            name=s.name, status=s.status, started_at=s.started_at,
            completed_at=s.completed_at, duration_ms=s.duration_ms,
        )
        for s in job.node_stages
    ]
    return StagesOut(research_id=job.research_id, status=job.status, current_stage=job.current_stage, stages=stages)


@router.post("/{research_id}/resume", response_model=ResumeRequestedOut | ResearchDisabledOut)
async def resume_research(research_id: str, principal=Depends(_VIEW)) -> ResumeRequestedOut | ResearchDisabledOut:
    """See service.resume_research()'s docstring for exactly what "resume"
    means here (a fresh research_id, re-submitted with identical
    parameters -- upstream checkpoints are keyed by
    ticker+date+config-signature, not by our own research_id, so there is
    no safe way to address "continue this exact research_id" more
    directly than that). MUST be async for the same reason POST
    /ai-research is (submit_research() calls asyncio.create_task())."""
    if not service.is_enabled():
        return ResearchDisabledOut()
    try:
        new_job, resumed_from_checkpoint = service.resume_research(research_id)
    except ResumeNotAllowedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return ResumeRequestedOut(
        original_research_id=research_id, new_research_id=new_job.research_id,
        status=new_job.status, resumed_from_checkpoint=resumed_from_checkpoint,
    )
