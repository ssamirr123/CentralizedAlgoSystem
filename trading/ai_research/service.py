"""
AIResearchService -- the only layer routes are allowed to call into
(router -> AIResearchService -> TradingAgentsAdapter -> TradingAgents).
Owns job creation, the QUEUED -> RUNNING -> ... -> COMPLETED/FAILED
lifecycle, concurrency limiting, and timeout protection.

Phase 5: every job/stage read and write goes through
trading.ai_research.repository (database-backed, sole source of truth --
Section 10). This module holds no job state of its own beyond the
concurrency semaphore, which is a rate-limiter, not a data store.

This module imports trading.ai_research.trading_agents_adapter (the
isolated adapter) and trading.ai_research.repository -- nothing else from
this package, and nothing from trading.common.broker,
trading.common.brokers.*, or trading.algos.* (see
tests/ai_research/test_isolation.py's AST scan, which covers this file
too).
"""
from __future__ import annotations

import asyncio
import logging

from trading.ai_research import repository
from trading.ai_research.config import load_ai_research_settings
from trading.ai_research.jobs import ResearchJob
from trading.ai_research.market.calendar_nse import TradingDateValidation, validate_research_date
from trading.ai_research.market.instruments import Market, resolve_instrument
from trading.ai_research.market.symbols import to_tradingagents_symbol
from trading.ai_research.schemas import (
    PortfolioContextIn,
    ResearchJobStatus,
    ResearchReportOut,
    ResearchRequestIn,
)
from trading.ai_research.trading_agents_adapter import (
    AIResearchConfigError,
    AIResearchDisabledError,
    PortfolioInput,
    PortfolioPositionInput,
    run_research,
)

logger = logging.getLogger("trading.ai_research")

# Bounds concurrent research runs. A module-level semaphore (not per-request)
# so the limit is shared across every caller in this process -- re-created
# if max_concurrent changes across calls (tests override it via
# _reset_semaphore()).
_semaphore: asyncio.Semaphore | None = None
_semaphore_size: int | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore, _semaphore_size
    settings = load_ai_research_settings()
    if _semaphore is None or _semaphore_size != settings.max_concurrent:
        _semaphore = asyncio.Semaphore(settings.max_concurrent)
        _semaphore_size = settings.max_concurrent
    return _semaphore


def _reset_semaphore() -> None:
    """Test-only: forces a fresh semaphore + module-level accounting on
    the next call, so tests changing AI_RESEARCH_MAX_CONCURRENT via
    monkeypatch aren't affected by a semaphore built for a previous
    test's value."""
    global _semaphore, _semaphore_size
    _semaphore = None
    _semaphore_size = None


def _sanitize_error(exc: Exception) -> str:
    """Never expose a stack trace or (accidentally) a credential/token
    fragment through the public API or the database. TradingAgents/LLM SDK
    exceptions are string-formatted by the adapter already
    (AIResearchConfigError's message); this trims it to a bounded length
    so an unexpectedly verbose provider error can't bloat every history
    read forever."""
    message = str(exc)
    return message[:1000]


def is_enabled() -> bool:
    settings = load_ai_research_settings()
    return settings.enabled and settings.ai_workloads_enabled


class NonTradingDateError(ValueError):
    """Section 19: raised instead of silently accepting/substituting a
    non-trading research_date for market=INDIA. Carries the full
    validation (reason + previous/next trading day) for the router to
    surface structured, useful feedback."""

    def __init__(self, validation: TradingDateValidation) -> None:
        super().__init__(validation.reason)
        self.validation = validation


def submit_research(request: ResearchRequestIn) -> ResearchJob:
    """Create a QUEUED, PERSISTED job and schedule its background
    execution. Returns immediately -- never runs TradingAgents inline."""
    if not is_enabled():
        raise AIResearchDisabledError("AI_RESEARCH_ENABLED is false")

    # Section 17: resolve the canonical instrument up front so an invalid
    # symbol/market is rejected before a job row is ever created.
    resolve_instrument(request.market, request.symbol)

    # Section 19: for market=INDIA, validate the research_date against
    # the real NSE trading calendar BEFORE creating anything -- never
    # silently change the requested date.
    if request.market == Market.INDIA:
        validation = validate_research_date(request.research_date)
        if not validation.is_valid:
            raise NonTradingDateError(validation)

    job = repository.create_run(request)
    asyncio.create_task(_run_job(job.research_id, request))
    logger.info(
        "ai_research.submitted research_id=%s symbol=%s depth=%s",
        job.research_id, request.symbol, request.research_depth.value,
    )
    return job


async def _run_job(research_id: str, request: ResearchRequestIn) -> None:
    settings = load_ai_research_settings()
    semaphore = _get_semaphore()

    async with semaphore:
        await asyncio.to_thread(repository.mark_running, research_id)
        started = asyncio.get_event_loop().time()
        logger.info("ai_research.running research_id=%s symbol=%s", research_id, request.symbol)

        portfolio_arg = None
        if request.portfolio is not None:
            portfolio_arg = _to_adapter_portfolio(request.portfolio)

        # Section 17: translate the canonical instrument into the symbol
        # TradingAgents' (yfinance-backed) data vendors actually expect --
        # TradingAgents itself is never responsible for our own market
        # normalization. US/generic requests resolve to the exact same
        # symbol they always did (Section 30 regression safety).
        instrument = resolve_instrument(request.market, request.symbol)
        provider_symbol = to_tradingagents_symbol(instrument)

        # Phase 8 (Section 23/29): for market=INDIA, source real technical
        # data through our own Breeze/TimescaleDB-backed service and
        # inject it into the graph's instrument_context -- never handing
        # TradingAgents Breeze credentials or letting it call Breeze
        # itself. market_data_provenance is metadata ABOUT this run
        # (never the candles) for Section 19/29 display; None for US.
        market_data_context: str | None = None
        market_data_provenance: dict | None = None
        if request.market == Market.INDIA:
            try:
                from trading.ai_research.market_data.service import build_market_data_context

                market_data_context, market_data_provenance = await asyncio.to_thread(
                    build_market_data_context, instrument, request.research_date,
                )
            except Exception:  # noqa: BLE001 -- market-data unavailability must never block research
                logger.warning("ai_research.market_data_context_failed research_id=%s", research_id, exc_info=True)

            # Phase 9 (Section 46): also expose deterministic, facts-only
            # options intelligence (ATM IV/PCR/Max Pain/OI levels/expected
            # move) for the primary NIFTY underlying -- never a strategy
            # recommendation (Section 47), and never for a historical
            # research_date (see build_options_market_context docstring).
            if instrument.canonical_id == "NIFTY_50":
                try:
                    from trading.market_data.options_service import build_options_market_context

                    options_context, options_provenance = await asyncio.to_thread(
                        build_options_market_context, "NIFTY", request.research_date,
                    )
                    if options_context:
                        market_data_context = "\n\n".join(filter(None, [market_data_context, options_context]))
                    market_data_provenance = market_data_provenance or {}
                    market_data_provenance["options_intelligence"] = options_provenance
                except Exception:  # noqa: BLE001 -- options-context unavailability must never block research
                    logger.warning("ai_research.options_context_failed research_id=%s", research_id, exc_info=True)

        def on_node_complete(node_name: str) -> None:
            try:
                repository.mark_node_complete(research_id, node_name)
            except Exception:  # noqa: BLE001 -- a bad node name/DB hiccup must never kill the run
                logger.debug("ai_research.stage_persist_error research_id=%s node=%s", research_id, node_name)

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    run_research,
                    provider_symbol,
                    request.research_date.isoformat(),
                    selected_analysts=tuple(a.value for a in request.selected_analysts) if request.selected_analysts else None,
                    llm_provider=request.llm_provider,
                    deep_think_llm=request.deep_think_llm,
                    quick_think_llm=request.quick_think_llm,
                    temperature=request.temperature,
                    debate_rounds=request.resolved_debate_rounds(),
                    risk_debate_rounds=request.resolved_risk_debate_rounds(),
                    retry_count=request.retry_count,
                    data_vendors=request.data_vendors,
                    checkpoint_enabled=request.checkpoint_enabled,
                    portfolio=portfolio_arg,
                    on_node_complete=on_node_complete,
                    market_data_context=market_data_context,
                ),
                timeout=settings.timeout_seconds,
            )
        except asyncio.TimeoutError:
            duration = asyncio.get_event_loop().time() - started
            logger.warning(
                "ai_research.timeout research_id=%s symbol=%s duration=%.1fs",
                research_id, request.symbol, duration,
            )
            await asyncio.to_thread(
                repository.mark_failed, research_id, f"research timed out after {settings.timeout_seconds}s",
                provider_error="TIMEOUT",
            )
            return
        except AIResearchConfigError as exc:
            duration = asyncio.get_event_loop().time() - started
            logger.warning(
                "ai_research.failed research_id=%s symbol=%s duration=%.1fs category=config_error provider_error=%s",
                research_id, request.symbol, duration, exc.provider_error,
            )
            await asyncio.to_thread(repository.mark_failed, research_id, _sanitize_error(exc), provider_error=exc.provider_error)
            return
        except Exception as exc:  # noqa: BLE001 -- last-resort: never let a job crash the event loop
            duration = asyncio.get_event_loop().time() - started
            logger.exception(
                "ai_research.failed research_id=%s symbol=%s duration=%.1fs category=unexpected",
                research_id, request.symbol, duration,
            )
            await asyncio.to_thread(repository.mark_failed, research_id, _sanitize_error(exc))
            return

        duration = asyncio.get_event_loop().time() - started
        report_out = ResearchReportOut(
            market_analysis=result.report.market_analysis,
            news_analysis=result.report.news_analysis,
            sentiment_analysis=result.report.sentiment_analysis,
            fundamentals_analysis=result.report.fundamentals_analysis,
            bull_case=result.report.bull_case,
            bear_case=result.report.bear_case,
            research_manager_decision=result.report.research_manager_decision,
            trader_plan=result.report.trader_plan,
            risk_aggressive=result.report.risk_aggressive,
            risk_conservative=result.report.risk_conservative,
            risk_neutral=result.report.risk_neutral,
            risk_judge_decision=result.report.risk_judge_decision,
            final_trade_decision=result.report.final_trade_decision,
            signal=result.report.signal,
        )
        try:
            await asyncio.to_thread(repository.mark_completed, research_id, report_out, market_data_provenance)
        except Exception:  # noqa: BLE001
            # Section 11: persistence of the final result failed -- the run
            # stays at whatever repository.mark_completed's own rollback
            # left it at (RUNNING), never silently reported COMPLETED.
            logger.exception("ai_research.final_persist_failed research_id=%s", research_id)
            return
        logger.info(
            "ai_research.completed research_id=%s symbol=%s duration=%.1fs signal=%s",
            research_id, request.symbol, duration, result.report.signal,
        )


def _to_adapter_portfolio(portfolio: PortfolioContextIn) -> PortfolioInput:
    return PortfolioInput(
        cash=portfolio.cash,
        currency=portfolio.currency,
        positions=tuple(
            PortfolioPositionInput(ticker=p.ticker, quantity=p.quantity, average_price=p.average_price)
            for p in portfolio.positions
        ),
    )


def get_job(research_id: str) -> ResearchJob | None:
    return repository.get_run(research_id)


def list_history(
    page: int = 1, page_size: int = 50, *, symbol: str | None = None, status: str | None = None,
    date_from=None, date_to=None, provider: str | None = None,
) -> tuple[list, int]:
    return repository.list_runs(
        page=page, page_size=page_size, symbol=symbol, status=status,
        date_from=date_from, date_to=date_to, provider=provider,
    )


class ResumeNotAllowedError(RuntimeError):
    """Raised when a resume is requested for a job that isn't a valid
    resume candidate."""


def resume_research(research_id: str) -> tuple[ResearchJob, bool]:
    """"Resume" means: re-submit a NEW, persisted research job with
    EXACTLY the original request's parameters, rebuilt from the DATABASE
    row (Section 15 -- this now works even after a backend restart, when
    no in-memory request object survives). If the original had
    checkpoint_enabled=True, upstream's own checkpoint_scope()/
    checkpoint_input() mechanism (keyed by ticker+trade_date+
    config-signature -- see trading_agents_adapter.py) transparently
    picks up an on-disk checkpoint and continues from the last successful
    node. If it was False, there is nothing to resume FROM -- this is a
    fresh re-run, and resumed_from_checkpoint=False says so honestly.

    Only a FAILED job may be resumed.
    """
    found = repository.get_request_for_resume(research_id)
    if found is None:
        raise ResumeNotAllowedError(f"unknown research_id: {research_id!r}")
    original_request, status = found
    if status != ResearchJobStatus.FAILED.value:
        raise ResumeNotAllowedError(f"only a FAILED job may be resumed (current status: {status})")

    resumed_from_checkpoint = bool(original_request.checkpoint_enabled)
    new_job = submit_research(original_request)
    logger.info(
        "ai_research.resume original_research_id=%s new_research_id=%s resumed_from_checkpoint=%s",
        research_id, new_job.research_id, resumed_from_checkpoint,
    )
    return new_job, resumed_from_checkpoint


def list_memory(limit: int = 50):
    from trading.ai_research.trading_agents_adapter import list_memory_entries
    return list_memory_entries(limit=limit)


def get_memory(memory_id: str):
    from trading.ai_research.trading_agents_adapter import get_memory_entry
    return get_memory_entry(memory_id)


def recover_interrupted_jobs() -> int:
    """Called once from the backend's own startup lifespan (Section 13) --
    see trading/api/app.py's lifespan()."""
    return repository.recover_interrupted_jobs()
