"""
AI Research Backtesting API -- read-mostly except for submit/resume/cancel.

    POST /api/ai-research/backtests                  submit a backtest (returns immediately)
    POST /api/ai-research/backtests/estimate          Section 24: run-count estimate, no job created
    GET  /api/ai-research/backtests                   paginated history
    GET  /api/ai-research/backtests/{id}              detail + aggregate metrics
    GET  /api/ai-research/backtests/{id}/status       lightweight lifecycle/progress poll
    GET  /api/ai-research/backtests/{id}/runs         paginated per-cell results (drill-down list)
    GET  /api/ai-research/backtests/{id}/metrics      aggregate "AI Research Decision Evaluation"
    GET  /api/ai-research/backtests/data-quality      Section 28: point-in-time safety/bias disclosure
    POST /api/ai-research/backtests/{id}/resume       continue unfinished cells only
    POST /api/ai-research/backtests/{id}/cancel       cooperative cancel (never destructive)

IMPORTANT ROUTING NOTE: this router is registered in trading/api/app.py
BEFORE the main ai_research_router, because that router's
GET /ai-research/{research_id} is a catch-all path param that would
otherwise match "/ai-research/backtests" with research_id="backtests"
(FastAPI matches routes in registration order across included routers).

Every route checks AI_RESEARCH_ENABLED first (Section: same feature flag
as single-run research -- backtesting is still TradingAgents-backed
research, never a separate always-on surface). No route accepts or
forwards anything broker/order/strategy-shaped -- BacktestRequestIn's
extra="forbid" rejects such fields before this module's code ever runs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading.ai_research.backtest import service
from trading.ai_research.backtest.schemas import (
    BacktestCancelledOut,
    BacktestCellOut,
    BacktestCellStatus,
    BacktestDetailOut,
    BacktestEstimateOut,
    BacktestHistoryEntryOut,
    BacktestHistoryPageOut,
    BacktestJobStatus,
    BacktestMetricsOut,
    BacktestRequestIn,
    BacktestResumedOut,
    BacktestRunsPageOut,
    BacktestStatusOut,
    BacktestSubmittedOut,
    DataQualityMatrixOut,
)
from trading.ai_research.repository import _aware
from trading.ai_research.schemas import ResearchDisabledOut
from trading.ai_research.trading_agents_adapter import AIResearchDisabledError
from trading.api.deps import enforce_rate_limit, require_permission
from trading.api.security.permissions import Permission

router = APIRouter(
    prefix="/ai-research/backtests", tags=["ai-research-backtests"],
    dependencies=[Depends(enforce_rate_limit)],
)

_VIEW = require_permission(Permission.VIEW)


def _job_to_detail(job) -> BacktestDetailOut:
    metrics = BacktestMetricsOut(**job.metrics) if job.metrics else None
    return BacktestDetailOut(
        backtest_id=job.backtest_id, status=BacktestJobStatus(job.status), symbols=list(job.symbols),
        start_date=job.start_date, end_date=job.end_date, frequency=job.frequency, market=job.market or "US",
        research_depth=job.research_depth, llm_provider=job.llm_provider, holding_days=job.holding_days,
        total_runs=job.total_runs, completed_runs=job.completed_runs, failed_runs=job.failed_runs,
        created_at=_aware(job.created_at), started_at=_aware(job.started_at), completed_at=_aware(job.completed_at),
        error=job.error_message, metrics=metrics,
    )


def _job_to_status(job) -> BacktestStatusOut:
    terminal = job.completed_runs + job.failed_runs
    progress = (terminal / job.total_runs) if job.total_runs else 0.0
    return BacktestStatusOut(
        backtest_id=job.backtest_id, status=BacktestJobStatus(job.status),
        total_runs=job.total_runs, completed_runs=job.completed_runs, failed_runs=job.failed_runs,
        remaining_runs=max(job.total_runs - terminal, 0), progress=round(progress, 4),
        current_symbol=job.current_symbol, current_date=job.current_date,
        started_at=_aware(job.started_at), completed_at=_aware(job.completed_at), error=job.error_message,
    )


def _cell_to_out(cell) -> BacktestCellOut:
    return BacktestCellOut(
        symbol=cell.symbol, cell_date=cell.cell_date, status=BacktestCellStatus(cell.status),
        research_id=cell.research_id, raw_decision=cell.raw_decision, normalized_decision=cell.normalized_decision,
        evaluation_status=cell.evaluation_status, raw_return=cell.raw_return, alpha_return=cell.alpha_return,
        benchmark=cell.benchmark, holding_days=cell.holding_days, resolution_date=cell.resolution_date,
        error=cell.error_message, started_at=_aware(cell.started_at), completed_at=_aware(cell.completed_at),
    )


@router.post("", response_model=BacktestSubmittedOut | ResearchDisabledOut)
async def submit_backtest(body: BacktestRequestIn, principal=Depends(_VIEW)) -> BacktestSubmittedOut | ResearchDisabledOut:
    # MUST be async: submit_backtest() calls asyncio.create_task() (same
    # reason POST /ai-research must be async -- see that router's docstring).
    if not service.is_enabled():
        return ResearchDisabledOut()
    try:
        job = service.submit_backtest(body)
    except AIResearchDisabledError:
        return ResearchDisabledOut()
    except service.BacktestLimitExceededError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return BacktestSubmittedOut(
        backtest_id=job.backtest_id, status=BacktestJobStatus(job.status),
        total_runs=job.total_runs, created_at=_aware(job.created_at),
    )


@router.post("/estimate", response_model=BacktestEstimateOut | ResearchDisabledOut)
def estimate_backtest(body: BacktestRequestIn, principal=Depends(_VIEW)) -> BacktestEstimateOut | ResearchDisabledOut:
    """Section 24: never creates a job -- pure computation against the
    same real date-grid function actual submission uses."""
    if not service.is_enabled():
        return ResearchDisabledOut()
    return service.estimate(body)


@router.get("/data-quality", response_model=DataQualityMatrixOut)
def get_data_quality(principal=Depends(_VIEW)) -> DataQualityMatrixOut:
    """Section 28: always available (doesn't require AI_RESEARCH_ENABLED)
    -- it describes the pinned TradingAgents version's own data-source
    behavior, not a live backtest result."""
    return DataQualityMatrixOut(**service.data_quality_matrix())


@router.get("", response_model=BacktestHistoryPageOut | ResearchDisabledOut)
def get_backtest_history(
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    status_filter: BacktestJobStatus | None = Query(None, alias="status"),
    principal=Depends(_VIEW),
) -> BacktestHistoryPageOut | ResearchDisabledOut:
    if not service.is_enabled():
        return ResearchDisabledOut()
    rows, total = service.list_backtests(page=page, page_size=page_size, status=status_filter.value if status_filter else None)
    items = [
        BacktestHistoryEntryOut(
            backtest_id=r.backtest_id, symbols=list(r.symbols), start_date=r.start_date, end_date=r.end_date,
            frequency=r.frequency, market=r.market or "US", status=BacktestJobStatus(r.status), total_runs=r.total_runs,
            completed_runs=r.completed_runs, failed_runs=r.failed_runs,
            created_at=_aware(r.created_at), completed_at=_aware(r.completed_at),
        )
        for r in rows
    ]
    return BacktestHistoryPageOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/{backtest_id}", response_model=BacktestDetailOut | ResearchDisabledOut)
def get_backtest(backtest_id: str, principal=Depends(_VIEW)) -> BacktestDetailOut | ResearchDisabledOut:
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_backtest(backtest_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown backtest_id: {backtest_id!r}")
    return _job_to_detail(job)


@router.get("/{backtest_id}/status", response_model=BacktestStatusOut | ResearchDisabledOut)
def get_backtest_status(backtest_id: str, principal=Depends(_VIEW)) -> BacktestStatusOut | ResearchDisabledOut:
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_backtest(backtest_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown backtest_id: {backtest_id!r}")
    return _job_to_status(job)


@router.get("/{backtest_id}/runs", response_model=BacktestRunsPageOut | ResearchDisabledOut)
def get_backtest_runs(
    backtest_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    symbol: str | None = None, principal=Depends(_VIEW),
) -> BacktestRunsPageOut | ResearchDisabledOut:
    if not service.is_enabled():
        return ResearchDisabledOut()
    if service.get_backtest(backtest_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown backtest_id: {backtest_id!r}")
    rows, total = service.list_cells(backtest_id, page=page, page_size=page_size, symbol=symbol)
    return BacktestRunsPageOut(items=[_cell_to_out(r) for r in rows], total=total, page=page, page_size=page_size)


@router.get("/{backtest_id}/metrics", response_model=BacktestMetricsOut | ResearchDisabledOut)
def get_backtest_metrics(backtest_id: str, principal=Depends(_VIEW)) -> BacktestMetricsOut | ResearchDisabledOut:
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_backtest(backtest_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown backtest_id: {backtest_id!r}")
    if job.metrics is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "metrics are not available until the backtest completes")
    return BacktestMetricsOut(**job.metrics)


@router.post("/{backtest_id}/resume", response_model=BacktestResumedOut | ResearchDisabledOut)
async def resume_backtest(backtest_id: str, principal=Depends(_VIEW)) -> BacktestResumedOut | ResearchDisabledOut:
    """MUST be async: resume_backtest() calls asyncio.create_task()."""
    if not service.is_enabled():
        return ResearchDisabledOut()
    try:
        job, pending_count = service.resume_backtest(backtest_id)
    except service.BacktestNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown backtest_id: {backtest_id!r}") from None
    except service.BacktestActionNotAllowedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return BacktestResumedOut(backtest_id=job.backtest_id, status=BacktestJobStatus.RUNNING, resumed_pending_cells=pending_count)


@router.post("/{backtest_id}/cancel", response_model=BacktestCancelledOut | ResearchDisabledOut)
def cancel_backtest(backtest_id: str, principal=Depends(_VIEW)) -> BacktestCancelledOut | ResearchDisabledOut:
    if not service.is_enabled():
        return ResearchDisabledOut()
    try:
        job = service.cancel_backtest(backtest_id)
    except service.BacktestNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown backtest_id: {backtest_id!r}") from None
    except service.BacktestActionNotAllowedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return BacktestCancelledOut(backtest_id=job.backtest_id, status=BacktestJobStatus(job.status))
