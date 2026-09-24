"""
Phase 10 -- AI Options Research API.

    POST /api/ai-options-research
    GET  /api/ai-options-research/preview
    GET  /api/ai-options-research/history
    GET  /api/ai-options-research/{research_id}
    GET  /api/ai-options-research/{research_id}/status
    GET  /api/ai-options-research/{research_id}/stages

Mirrors trading/ai_research/router.py's exact conventions: the disabled
flag returns HTTP 200 with a disabled payload (never an error), auth via
require_permission(Permission.VIEW), rate-limited at the router level.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading.ai_options_research import service
from trading.ai_options_research.context import evidence_quality_gate, freeze_snapshot
from trading.ai_options_research.jobs import OptionsResearchJob
from trading.ai_options_research.schemas import (
    HistoryItemOut,
    HistoryOut,
    OptionsResearchRequestIn,
    ResearchDisabledOut,
    ResearchResultOut,
    ResearchStatusOut,
    SnapshotPreviewOut,
    StageOut,
    StagesOut,
)
from trading.api.deps import Principal, enforce_rate_limit, require_permission
from trading.api.security.permissions import Permission
from trading.market_data import option_chain as oc
from trading.market_data.options_intelligence import build_market_structure_summary
from trading.market_data.options_service import get_live_chain_from_cache, get_live_chain_via_provider
from trading.market_data.service import get_service
from trading.market_data.underlying_config import UNDERLYING_CONFIGS

router = APIRouter(prefix="/ai-options-research", tags=["ai-options-research"], dependencies=[Depends(enforce_rate_limit)])
_VIEW = require_permission(Permission.VIEW)


def _job_to_status(job: OptionsResearchJob) -> ResearchStatusOut:
    return ResearchStatusOut(
        research_id=job.research_id, underlying=job.underlying, status=job.status,
        current_stage=job.current_stage, created_at=job.created_at, started_at=job.started_at,
        completed_at=job.completed_at, error=job.error, provider_error=job.provider_error,
        research_snapshot_id=job.research_snapshot_id, evidence_quality=job.evidence_quality,
    )


def _job_to_result(job: OptionsResearchJob) -> ResearchResultOut:
    base = _job_to_status(job).model_dump()
    return ResearchResultOut(**base, report=job.report)


@router.get("/preview", response_model=SnapshotPreviewOut | ResearchDisabledOut)
def preview_snapshot(
    underlying: str = Query("NIFTY"), expiry: str = Query("nearest"), strike_window: int = Query(default=10, ge=1, le=50),
    principal: Principal = Depends(_VIEW),
):
    if not service.is_enabled():
        return ResearchDisabledOut()
    under = underlying.strip().upper()
    if under not in UNDERLYING_CONFIGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported underlying {underlying!r}")
    master = get_service().master
    resolved_expiry = oc.resolve_expiry(master, under, expiry)
    if resolved_expiry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No expiries known yet for {under}")

    svc = get_service()
    result = get_live_chain_from_cache(under, resolved_expiry, strike_window=strike_window, cache=svc.cache, master=svc.master)
    if not any(r.call is not None or r.put is not None for r in result.chain.rows):
        try:
            result = get_live_chain_via_provider(under, resolved_expiry, strike_window=strike_window)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Provider fetch failed: {type(exc).__name__}") from None

    from trading.core.config import load_settings

    settings = load_settings()
    summary = build_market_structure_summary(
        result.chain, as_of=None, risk_free_rate=settings.options_risk_free_rate,
        all_expiries=master.list_expiries(under) or [resolved_expiry],
        underlying_source=result.source, option_chain_source=result.source, provider_call_count=result.provider_call_count,
    )
    context = freeze_snapshot(summary)
    level, reason = evidence_quality_gate(context)
    return SnapshotPreviewOut(
        underlying=context.underlying, spot=context.spot, expiry=context.expiry, atm_strike=context.atm_strike,
        dte_calendar=context.dte_calendar, dte_trading=context.dte_trading, oi_pcr=context.oi_pcr,
        max_pain_strike=context.max_pain_strike, atm_iv=context.atm_iv, expected_move=context.expected_move,
        oi_support=list(context.oi_support), oi_resistance=list(context.oi_resistance),
        data_quality=context.data_quality.value, data_quality_reasons=list(context.data_quality_reasons),
        evidence_quality=level, evidence_quality_reason=reason,
    )


@router.post("", response_model=ResearchStatusOut | ResearchDisabledOut)
async def submit_options_research(request: OptionsResearchRequestIn, principal: Principal = Depends(_VIEW)):
    if not service.is_enabled():
        return ResearchDisabledOut()
    try:
        job = service.submit_research(request)
    except service.OptionsResearchDisabledError:
        return ResearchDisabledOut()
    except service.OptionsResearchConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return _job_to_status(job)


@router.get("/history", response_model=HistoryOut | ResearchDisabledOut)
def get_history(
    page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
    underlying: str | None = Query(default=None), status_filter: str | None = Query(default=None, alias="status"),
    principal: Principal = Depends(_VIEW),
):
    if not service.is_enabled():
        return ResearchDisabledOut()
    jobs, total = service.list_history(page, page_size, underlying=underlying, status=status_filter)
    items = [
        HistoryItemOut(
            research_id=j.research_id, underlying=j.underlying, expiry=j.expiry.isoformat() if j.expiry else None,
            status=j.status, market_regime_trend=(j.report or {}).get("market_regime", {}).get("trend"),
            candidate_count=len((j.report or {}).get("candidates", [])), created_at=j.created_at,
        )
        for j in jobs
    ]
    return HistoryOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/{research_id}", response_model=ResearchResultOut | ResearchDisabledOut)
def get_result(research_id: str, principal: Principal = Depends(_VIEW)):
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_job(research_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown research_id")
    return _job_to_result(job)


@router.get("/{research_id}/status", response_model=ResearchStatusOut | ResearchDisabledOut)
def get_status(research_id: str, principal: Principal = Depends(_VIEW)):
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_job(research_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown research_id")
    return _job_to_status(job)


@router.get("/{research_id}/stages", response_model=StagesOut | ResearchDisabledOut)
def get_stages(research_id: str, principal: Principal = Depends(_VIEW)):
    if not service.is_enabled():
        return ResearchDisabledOut()
    job = service.get_job(research_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown research_id")
    return StagesOut(
        stages=[StageOut(name=s.name, status=s.status, started_at=s.started_at, completed_at=s.completed_at) for s in job.node_stages],
        current_stage=job.current_stage,
    )
