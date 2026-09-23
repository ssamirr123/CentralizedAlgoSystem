"""
Phase 10 -- job lifecycle orchestration, mirroring
``trading/ai_research/service.py``'s asyncio pattern (semaphore-bounded
concurrency, ``asyncio.create_task`` fire-and-forget submission,
``asyncio.wait_for(timeout=...)``, a per-stage completion callback).

This is the ONE place the whole Section-1 pipeline is wired together:
    Market Data -> Options Intelligence -> AI Options Research
        -> Candidate Structures -> Risk Analysis -> Research Report -> STOP
No function in this module can reach a broker, order, or strategy API --
none of that surface is imported here.
"""
from __future__ import annotations

import asyncio
import logging

from trading.ai_options_research import agents, repository
from trading.ai_options_research.agent_schemas import CandidateCommentary, CandidateRiskAnalysis
from trading.ai_options_research.candidates import CandidateGenerationConfig, StrategyCandidate, generate_candidates
from trading.ai_options_research.context import EvidenceQuality, evidence_quality_gate, freeze_snapshot
from trading.ai_options_research.jobs import OptionsResearchJob
from trading.ai_options_research.payoff import PayoffResult
from trading.ai_options_research.schemas import OptionsResearchRequestIn
from trading.ai_research.config import load_ai_research_settings
from trading.market_data import option_chain as oc
from trading.market_data.options_intelligence import build_market_structure_summary
from trading.market_data.options_service import (
    OptionsDataError,
    get_historical_chain,
    get_live_chain_from_cache,
    get_live_chain_via_provider,
)
from trading.market_data.service import get_service
from trading.market_data.underlying_config import UNDERLYING_CONFIGS

logger = logging.getLogger("trading.ai_options_research")

_semaphore: asyncio.Semaphore | None = None
_semaphore_size: int | None = None


class OptionsResearchDisabledError(RuntimeError):
    pass


class OptionsResearchConfigError(RuntimeError):
    def __init__(self, message: str, *, error_category: str | None = None) -> None:
        super().__init__(message)
        self.error_category = error_category


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore, _semaphore_size
    settings = load_ai_research_settings()
    if _semaphore is None or _semaphore_size != settings.ai_options_max_concurrent:
        _semaphore = asyncio.Semaphore(settings.ai_options_max_concurrent)
        _semaphore_size = settings.ai_options_max_concurrent
    return _semaphore


def _reset_semaphore() -> None:
    global _semaphore, _semaphore_size
    _semaphore, _semaphore_size = None, None


def is_enabled() -> bool:
    settings = load_ai_research_settings()
    return settings.enabled and settings.ai_workloads_enabled and settings.ai_options_research_enabled


def submit_research(request: OptionsResearchRequestIn) -> OptionsResearchJob:
    if not is_enabled():
        raise OptionsResearchDisabledError("AI Options Research is disabled")
    under = request.underlying.strip().upper()
    if under not in UNDERLYING_CONFIGS:
        raise OptionsResearchConfigError(
            f"Unsupported underlying {request.underlying!r}. Supported: {', '.join(sorted(UNDERLYING_CONFIGS))}",
            error_category="UNDERLYING_DATA_MISSING",
        )
    settings = load_ai_research_settings()
    if request.candidate_limit > settings.ai_options_max_candidates:
        raise OptionsResearchConfigError(
            f"candidate_limit {request.candidate_limit} exceeds AI_OPTIONS_MAX_CANDIDATES={settings.ai_options_max_candidates}",
            error_category="COST_GUARD",
        )
    if request.debate_rounds > settings.ai_options_max_debate_rounds:
        raise OptionsResearchConfigError(
            f"debate_rounds {request.debate_rounds} exceeds AI_OPTIONS_MAX_DEBATE_ROUNDS={settings.ai_options_max_debate_rounds}",
            error_category="COST_GUARD",
        )
    job = repository.create_run(request)
    asyncio.create_task(_run_job(job.research_id, request))
    return job


def _classify_llm_error(exc: Exception) -> str | None:
    text = str(exc).lower()
    if "rate_limit" in text or "429" in text or "too many requests" in text:
        return "LLM_RATE_LIMIT"
    if "auth" in text or "401" in text or "invalid api key" in text or "unauthorized" in text:
        return "LLM_AUTH"
    if "timeout" in text or "timed out" in text:
        return "LLM_TIMEOUT"
    return None


def _resolve_chain(request: OptionsResearchRequestIn):
    under = request.underlying.strip().upper()
    master = get_service().master
    resolved_expiry = oc.resolve_expiry(master, under, request.expiry)
    if resolved_expiry is None:
        raise OptionsResearchConfigError(f"No expiries known yet for {under}", error_category="EXPIRY_NOT_FOUND")

    if request.as_of is not None:
        from trading.database.connection import SessionLocal

        with SessionLocal() as db:
            try:
                result = get_historical_chain(db, under, resolved_expiry, request.as_of, strike_window=request.strike_window)
            except OptionsDataError as exc:
                raise OptionsResearchConfigError(str(exc), error_category="SNAPSHOT_NOT_FOUND") from exc
        return result, resolved_expiry, master.list_expiries(under)

    svc = get_service()
    result = get_live_chain_from_cache(under, resolved_expiry, strike_window=request.strike_window, cache=svc.cache, master=svc.master)
    if not any(r.call is not None or r.put is not None for r in result.chain.rows):
        try:
            result = get_live_chain_via_provider(under, resolved_expiry, strike_window=request.strike_window)
        except Exception as exc:  # noqa: BLE001
            raise OptionsResearchConfigError(f"Provider fetch failed: {exc}", error_category="MARKET_DATA_MISSING") from exc
    return result, resolved_expiry, master.list_expiries(under)


def _serialize_payoff(payoff: PayoffResult | None) -> dict | None:
    if payoff is None:
        return None
    return {
        "net_premium": payoff.net_premium, "max_profit": payoff.max_profit,
        "max_profit_unbounded": payoff.max_profit_unbounded, "max_loss": payoff.max_loss,
        "max_loss_unbounded": payoff.max_loss_unbounded, "breakevens": list(payoff.breakevens),
        "pricing_method": payoff.pricing_method, "priced": payoff.priced,
        "payoff_curve": [{"underlying_price": p.underlying_price, "pnl": p.pnl} for p in payoff.payoff_curve],
    }


def _serialize_candidate(c: StrategyCandidate, commentary: CandidateCommentary | None, risk: CandidateRiskAnalysis | None) -> dict:
    return {
        "strategy": c.strategy.value, "supported": c.supported,
        "legs": [
            {
                "side": l.side.value, "option_type": l.option_type.value, "strike": l.strike, "price": l.price,
                "price_source": l.price_source, "delta": l.delta, "iv": l.iv, "volume": l.volume,
                "open_interest": l.open_interest, "bid": l.bid, "ask": l.ask,
                "liquidity_ok": l.liquidity_ok, "liquidity_reasons": list(l.liquidity_reasons),
            }
            for l in c.legs
        ],
        "payoff": _serialize_payoff(c.payoff), "liquidity_ok": c.liquidity_ok,
        "generation_notes": list(c.generation_notes), "unsupported_reason": c.unsupported_reason,
        "commentary": commentary.model_dump() if commentary else None,
        "risk_analysis": risk.model_dump() if risk else None,
    }


def _run_pipeline(research_id: str, request: OptionsResearchRequestIn) -> None:
    warnings: list[str] = []
    try:
        chain_result, resolved_expiry, all_expiries = _resolve_chain(request)
    except OptionsResearchConfigError as exc:
        repository.mark_failed(research_id, str(exc), error_category=exc.error_category)
        repository.mark_remaining_stages_skipped(research_id)
        return

    settings_ai = load_ai_research_settings()
    from trading.core.config import load_settings

    market_settings = load_settings()
    summary = build_market_structure_summary(
        chain_result.chain, as_of=request.as_of, risk_free_rate=market_settings.options_risk_free_rate,
        all_expiries=all_expiries or [resolved_expiry], underlying_source=chain_result.source,
        option_chain_source=chain_result.source, provider_call_count=chain_result.provider_call_count,
    )
    context = freeze_snapshot(summary)
    evidence_level, evidence_reason = evidence_quality_gate(context)
    repository.mark_stage_complete(research_id, "Snapshot Validation")

    config = CandidateGenerationConfig(
        min_delta=request.min_delta, max_delta=request.max_delta, minimum_volume=request.minimum_volume,
        minimum_oi=request.minimum_oi, maximum_bid_ask_spread=request.maximum_bid_ask_spread,
        wing_width=request.wing_width, hedge_wing_width=request.hedge_wing_width,
        strike_window=request.strike_window,
    )
    candidates = generate_candidates(
        request.resolved_strategy_universe(), summary, config, limit=request.candidate_limit,
    )

    if evidence_level == EvidenceQuality.INSUFFICIENT:
        report = {
            "market_overview": "INSUFFICIENT DATA -- AI options research was not performed.",
            "market_regime": {}, "technical_context": "", "news_sentiment": "Not available in this phase",
            "options_market_structure": {"reason": evidence_reason}, "volatility_analysis": {},
            "oi_positioning_analysis": {}, "bull_case": {}, "bear_case": {},
            "candidates": [_serialize_candidate(c, None, None) for c in candidates],
            "invalidation_conditions": [], "final_summary": f"Insufficient data: {evidence_reason}",
            "sanitizer_warnings": [],
        }
        repository.mark_remaining_stages_skipped(research_id)
        repository.mark_completed(research_id, report, research_snapshot_id=context.research_snapshot_id, evidence_quality=evidence_level)
        return

    try:
        llm = agents.get_llm(
            request.llm_provider or settings_ai.llm_provider, request.quick_think_llm or "gpt-4o-mini",
            temperature=request.temperature,
        )

        regime = agents.run_market_regime_agent(context, llm)
        repository.mark_stage_complete(research_id, "Market Regime")

        volatility = agents.run_volatility_agent(context, llm)
        repository.mark_stage_complete(research_id, "Volatility")

        oi_positioning = agents.run_oi_positioning_agent(context, llm)
        repository.mark_stage_complete(research_id, "OI Positioning")

        repository.mark_stage_complete(research_id, "Candidate Generation")

        strategy_research, w = agents.run_strategy_research_agent(context, candidates, regime, volatility, oi_positioning, llm)
        warnings.extend(w)
        repository.mark_stage_complete(research_id, "Strategy Research")

        bull, bear, w = agents.run_debate(context, candidates, llm, rounds=request.debate_rounds)
        warnings.extend(w)
        repository.mark_stage_complete(research_id, "Bull/Bear Research")

        risk, w = agents.run_risk_agent(context, candidates, llm)
        warnings.extend(w)
        repository.mark_stage_complete(research_id, "Risk")

        deep_llm = agents.get_llm(
            request.llm_provider or settings_ai.llm_provider, request.deep_think_llm or request.quick_think_llm or "gpt-4o-mini",
            temperature=request.temperature,
        )
        final = agents.run_coordinator(context, candidates, regime, volatility, oi_positioning, bull, bear, risk, deep_llm)
        repository.mark_stage_complete(research_id, "Final Coordinator")
    except Exception as exc:  # noqa: BLE001
        category = _classify_llm_error(exc) or "LLM_ERROR"
        repository.mark_failed(
            research_id, f"{type(exc).__name__}: {exc}", error_category=category,
            research_snapshot_id=context.research_snapshot_id, evidence_quality=evidence_level,
        )
        repository.mark_remaining_stages_skipped(research_id)
        return

    commentary_by_idx = {c.candidate_index: c for c in strategy_research.commentary}
    risk_by_idx = {r.candidate_index: r for r in risk.analyses}
    report = {
        "market_overview": final.market_overview, "market_regime": regime.model_dump(),
        "technical_context": final.technical_context, "news_sentiment": final.news_sentiment,
        "options_market_structure": {"summary": final.options_market_structure_summary},
        "volatility_analysis": volatility.model_dump(), "oi_positioning_analysis": oi_positioning.model_dump(),
        "bull_case": bull.model_dump(), "bear_case": bear.model_dump(),
        "candidates": [_serialize_candidate(c, commentary_by_idx.get(i), risk_by_idx.get(i)) for i, c in enumerate(candidates)],
        "invalidation_conditions": final.invalidation_conditions, "final_summary": final.final_summary,
        "sanitizer_warnings": warnings,
    }
    repository.mark_completed(research_id, report, research_snapshot_id=context.research_snapshot_id, evidence_quality=evidence_level)


async def _run_job(research_id: str, request: OptionsResearchRequestIn) -> None:
    settings = load_ai_research_settings()
    semaphore = _get_semaphore()
    async with semaphore:
        await asyncio.to_thread(repository.mark_running, research_id)
        try:
            await asyncio.wait_for(asyncio.to_thread(_run_pipeline, research_id, request), timeout=settings.ai_options_timeout_seconds)
        except asyncio.TimeoutError:
            await asyncio.to_thread(repository.mark_failed, research_id, "Research timed out", error_category="LLM_TIMEOUT")
            await asyncio.to_thread(repository.mark_remaining_stages_skipped, research_id)
        except Exception as exc:  # noqa: BLE001 -- last-resort guard, never leaves a job stuck RUNNING
            logger.exception("ai_options_research.pipeline_crashed research_id=%s", research_id)
            await asyncio.to_thread(repository.mark_failed, research_id, f"{type(exc).__name__}: {exc}")
            await asyncio.to_thread(repository.mark_remaining_stages_skipped, research_id)


def get_job(research_id: str) -> OptionsResearchJob | None:
    return repository.get_run(research_id)


def list_history(page: int = 1, page_size: int = 25, *, underlying: str | None = None, status: str | None = None):
    return repository.list_runs(page, page_size, underlying=underlying, status=status)


def recover_interrupted_jobs() -> int:
    return repository.recover_interrupted_jobs()
