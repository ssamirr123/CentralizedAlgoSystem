"""
AIBacktestService -- orchestrates a grid of independent single-research
runs and their evaluation. The only layer backtest/router.py calls into.

Architecture (Section 1, never inverted):
    Historical Dates -> TradingAgents -> Historical AI Decisions
                      -> Evaluation -> Metrics/Report

Every cell reuses trading_agents_adapter.run_research() -- the SAME
function a normal single research job calls -- and is ALSO persisted as a
first-class trading.ai_research.repository AIResearchRun row (Section 18:
traceability/drill-down through the existing Phase 4 UI, no duplicated
report storage). This module imports trading.ai_research.repository and
trading.ai_research.trading_agents_adapter only -- nothing from
trading.common.broker, trading.common.brokers.*, or trading.algos.* (see
tests/ai_research/test_isolation.py's AST scan, which covers this file).
"""
from __future__ import annotations

import asyncio
import logging

from trading.ai_research import repository as research_repository
from trading.ai_research.backtest import evaluator
from trading.ai_research.backtest import repository as backtest_repository
from trading.ai_research.backtest.models import AIBacktestJob
from trading.ai_research.backtest.schemas import (
    BacktestEstimateOut,
    BacktestLimitsOut,
    BacktestRequestIn,
)
from trading.ai_research.config import load_ai_research_settings
from trading.ai_research.schemas import ResearchReportOut, ResearchRequestIn
from trading.ai_research.trading_agents_adapter import (
    AIResearchConfigError,
    AIResearchDisabledError,
    backtest_date_grid,
    settle_pending_decisions,
)

logger = logging.getLogger("trading.ai_research.backtest")

_semaphore: asyncio.Semaphore | None = None
_semaphore_size: int | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore, _semaphore_size
    settings = load_ai_research_settings()
    if _semaphore is None or _semaphore_size != settings.ai_backtest_max_concurrent:
        _semaphore = asyncio.Semaphore(settings.ai_backtest_max_concurrent)
        _semaphore_size = settings.ai_backtest_max_concurrent
    return _semaphore


def _reset_semaphore() -> None:
    """Test-only, mirrors ai_research.service._reset_semaphore()."""
    global _semaphore, _semaphore_size
    _semaphore = None
    _semaphore_size = None


class BacktestLimitExceededError(RuntimeError):
    """Raised when a request exceeds a configured cost/workload guard
    (Section 7) -- the request is REJECTED, never silently truncated."""


class BacktestNotFoundError(RuntimeError):
    pass


class BacktestActionNotAllowedError(RuntimeError):
    """Raised by resume/cancel when the backtest isn't in a state that
    action applies to."""


def is_enabled() -> bool:
    settings = load_ai_research_settings()
    return settings.enabled and settings.ai_workloads_enabled and settings.ai_backtest_enabled


def _limits() -> BacktestLimitsOut:
    settings = load_ai_research_settings()
    return BacktestLimitsOut(
        max_symbols=settings.ai_backtest_max_symbols,
        max_dates=settings.ai_backtest_max_dates,
        max_runs=settings.ai_backtest_max_runs,
        max_concurrent=settings.ai_backtest_max_concurrent,
        default_holding_days=settings.ai_backtest_default_holding_days,
    )


def _grid_and_check(request: BacktestRequestIn) -> tuple[tuple[str, ...], BacktestLimitsOut, str | None]:
    """Computes the real date grid and checks it against configured
    limits (Section 7) WITHOUT creating anything. Returns
    (dates, limits, violation_message_or_None)."""
    limits = _limits()
    dates = backtest_date_grid(
        request.start_date.isoformat(), request.end_date.isoformat(), request.frequency.value,
        market=request.market.value,
    )

    violation = None
    if len(request.symbols) > limits.max_symbols:
        violation = f"{len(request.symbols)} symbols exceeds the configured limit of {limits.max_symbols} (AI_BACKTEST_MAX_SYMBOLS)"
    elif len(dates) > limits.max_dates:
        violation = f"{len(dates)} dates exceeds the configured limit of {limits.max_dates} (AI_BACKTEST_MAX_DATES)"
    else:
        total = len(request.symbols) * len(dates)
        if total > limits.max_runs:
            violation = (
                f"{len(request.symbols)} symbols x {len(dates)} dates = {total} research runs "
                f"exceeds the configured limit of {limits.max_runs} (AI_BACKTEST_MAX_RUNS)"
            )
    return dates, limits, violation


def estimate(request: BacktestRequestIn) -> BacktestEstimateOut:
    """Section 24: a run-count estimate, computed from the SAME grid
    function actual submission uses. Never creates a job."""
    dates, limits, violation = _grid_and_check(request)
    return BacktestEstimateOut(
        total_runs=len(request.symbols) * len(dates),
        symbol_count=len(request.symbols),
        date_count=len(dates),
        dates=[__import__("datetime").date.fromisoformat(d) for d in dates],
        exceeds_limit=violation is not None,
        limit_message=violation,
        limits=limits,
    )


def submit_backtest(request: BacktestRequestIn) -> AIBacktestJob:
    if not is_enabled():
        raise AIResearchDisabledError("AI_RESEARCH_ENABLED is false -- backtesting will not run.")

    dates, limits, violation = _grid_and_check(request)
    if violation:
        raise BacktestLimitExceededError(violation)
    if not dates:
        raise BacktestLimitExceededError("the requested date range produced zero research dates (all in the future?)")

    holding_days = request.holding_days or limits.default_holding_days
    backtest_id = backtest_repository.create_backtest(request, dates, holding_days=holding_days)
    job = backtest_repository.get_backtest(backtest_id)
    asyncio.create_task(_run_backtest(backtest_id))
    logger.info(
        "ai_research.backtest.submitted backtest_id=%s symbols=%s dates=%d",
        backtest_id, request.symbols, len(dates),
    )
    return job


def _build_research_request(job: AIBacktestJob, symbol: str, cell_date: str) -> ResearchRequestIn:
    return ResearchRequestIn(
        symbol=symbol,
        research_date=__import__("datetime").date.fromisoformat(cell_date),
        market=job.market or "US",
        research_depth=job.research_depth,
        selected_analysts=job.selected_analysts,
        llm_provider=job.llm_provider,
        deep_think_llm=job.deep_think_llm,
        quick_think_llm=job.quick_think_llm,
        debate_rounds=job.debate_rounds,
        risk_debate_rounds=job.risk_debate_rounds,
        retry_count=job.retry_count,
        data_vendors=job.data_vendors,
        # Backtest-level PENDING/COMPLETED tracking (Section 20) replaces
        # upstream's checkpoint mechanism for resume -- checkpoints are for
        # crash recovery of a single in-flight run, not for a multi-cell
        # sweep, and Phase 3 already proved research_id-keyed checkpoint
        # resume doesn't exist upstream.
        checkpoint_enabled=False,
        portfolio=None,
    )


async def _run_one_cell(job: AIBacktestJob, symbol: str, cell_date: str) -> None:
    from trading.ai_research.market.instruments import resolve_instrument
    from trading.ai_research.market.symbols import to_tradingagents_symbol
    from trading.ai_research.trading_agents_adapter import run_research

    backtest_repository.mark_cell_running(job.backtest_id, symbol, cell_date)
    request = _build_research_request(job, symbol, cell_date)
    research_job = await asyncio.to_thread(research_repository.create_run, request)
    research_id = research_job.research_id
    await asyncio.to_thread(research_repository.mark_running, research_id)

    # Section 17: same canonical -> provider translation single-run
    # research uses -- TradingAgents is never responsible for our own
    # market normalization.
    instrument = resolve_instrument(request.market, symbol)
    provider_symbol = to_tradingagents_symbol(instrument)

    # Section 25: backtesting uses the SAME Market Data Service for
    # research input as single-run research (Section 23) -- the research
    # date cutoff keeps this strictly separate from the evaluation-window
    # candles upstream's own settle_pending_decisions() fetches later
    # (Phase 6, untouched by this phase).
    market_data_context = None
    market_data_provenance = None
    if request.market.value == "INDIA":
        try:
            from datetime import date as _date

            from trading.ai_research.market_data.service import build_market_data_context

            market_data_context, market_data_provenance = await asyncio.to_thread(
                build_market_data_context, instrument, _date.fromisoformat(cell_date),
            )
        except Exception:  # noqa: BLE001 -- market-data unavailability must never block a backtest cell
            logger.warning("ai_research.backtest.market_data_context_failed backtest_id=%s symbol=%s date=%s",
                            job.backtest_id, symbol, cell_date, exc_info=True)

    try:
        result = await asyncio.to_thread(
            run_research,
            provider_symbol, cell_date,
            selected_analysts=tuple(a.value for a in request.selected_analysts) if request.selected_analysts else None,
            llm_provider=request.llm_provider,
            deep_think_llm=request.deep_think_llm,
            quick_think_llm=request.quick_think_llm,
            debate_rounds=request.resolved_debate_rounds(),
            risk_debate_rounds=request.resolved_risk_debate_rounds(),
            retry_count=request.retry_count,
            data_vendors=request.data_vendors,
            checkpoint_enabled=False,
            market_data_context=market_data_context,
        )
    except AIResearchConfigError as exc:
        error = str(exc)[:1000]
        await asyncio.to_thread(research_repository.mark_failed, research_id, error, provider_error=exc.provider_error)
        backtest_repository.mark_cell_failed(job.backtest_id, symbol, cell_date, error)
        logger.warning(
            "ai_research.backtest.cell_failed backtest_id=%s symbol=%s date=%s error=%s",
            job.backtest_id, symbol, cell_date, error,
        )
        return
    except Exception as exc:  # noqa: BLE001 -- one cell's failure must not abort the sweep (Section 21)
        error = str(exc)[:1000]
        await asyncio.to_thread(research_repository.mark_failed, research_id, error)
        backtest_repository.mark_cell_failed(job.backtest_id, symbol, cell_date, error)
        logger.exception("ai_research.backtest.cell_unexpected_failure backtest_id=%s symbol=%s date=%s", job.backtest_id, symbol, cell_date)
        return

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
        await asyncio.to_thread(research_repository.mark_completed, research_id, report_out, market_data_provenance)
    except Exception:  # noqa: BLE001 -- Section 11 precedent: never claim COMPLETED on a failed persist
        logger.exception("ai_research.backtest.cell_persist_failed backtest_id=%s research_id=%s", job.backtest_id, research_id)
        backtest_repository.mark_cell_failed(job.backtest_id, symbol, cell_date, "failed to persist research result")
        return

    from trading.ai_research.trading_agents_adapter import get_memory_entry

    # The decision-memory log is keyed by whatever ticker string
    # run_research() was actually called with -- provider_symbol, not the
    # canonical symbol (e.g. "^NSEI", not "NIFTY 50") -- so this lookup
    # must use the same one or it silently finds nothing for INDIA cells.
    entry = await asyncio.to_thread(get_memory_entry, f"{cell_date}:{provider_symbol}")
    normalized = entry.rating if entry else None
    backtest_repository.mark_cell_completed(
        job.backtest_id, symbol, cell_date, research_id=research_id,
        raw_decision=result.report.final_trade_decision, normalized_decision=normalized,
    )


async def _settle_symbol(job: AIBacktestJob, provider_symbol: str) -> None:
    """Flushes provider_symbol's last pending decision via upstream's REAL
    settle_pending() (Section 13/21: a settlement failure -- e.g. a
    provider rate limit on the reflection call -- must not abort the
    caller). MUST be the same ticker string run_research() was called
    with (the memory log is keyed by that, not our canonical symbol --
    Section 17)."""
    try:
        await asyncio.to_thread(
            settle_pending_decisions, provider_symbol,
            llm_provider=job.llm_provider, quick_think_llm=job.quick_think_llm,
            data_vendors=job.data_vendors, holding_days=job.holding_days,
        )
    except Exception:  # noqa: BLE001
        logger.warning("ai_research.backtest.settle_failed backtest_id=%s symbol=%s", job.backtest_id, provider_symbol, exc_info=True)


def _persist_evaluation_from_log(backtest_id: str, symbol: str, provider_symbol: str, cell_date: str) -> None:
    """Re-reads TradingMemoryLog directly for one (symbol, date) cell and
    persists raw_return/alpha_return if upstream has resolved it (its
    percent-string fields, exactly like tradingagents.backtest._alpha()/
    summarize() read them -- MemoryEntry itself doesn't expose raw/alpha/
    holding/resolved, only the normalized rating/decision/reflection
    shape, so this reads the log's parsed entries one level down). Cells
    whose holding window hasn't fully traded yet stay PENDING honestly
    (Section 13: never fabricate with today's price)."""
    from pathlib import Path

    from trading.ai_research.config import load_ai_research_settings
    from trading.ai_research.trading_agents_adapter import _memory_log_path

    settings = load_ai_research_settings()
    path = _memory_log_path(settings.data_dir)
    if not Path(path).exists():
        return
    try:
        from tradingagents.agents.utils.memory import TradingMemoryLog
    except ImportError:
        return

    log = TradingMemoryLog({"memory_log_path": str(path)})
    for entry in log.load_entries():
        if entry.get("ticker") != provider_symbol or entry.get("date") != cell_date or entry.get("pending"):
            continue
        raw_pct, alpha_pct, holding = entry.get("raw"), entry.get("alpha"), entry.get("holding")
        if not raw_pct or not alpha_pct or not holding:
            return
        try:
            raw_return = float(raw_pct.strip().rstrip("%")) / 100
            alpha_return = float(alpha_pct.strip().rstrip("%")) / 100
            holding_days = int(str(holding).rstrip("d"))
        except (ValueError, AttributeError):
            return
        resolution_date = entry.get("resolved") or cell_date
        # We never expose config["benchmark_ticker"]/benchmark_map overrides
        # in BacktestRequestIn, so upstream's own _resolve_benchmark()
        # always falls through to its documented US-ticker default: "SPY".
        backtest_repository.mark_cell_evaluated(
            backtest_id, symbol, cell_date, raw_return=raw_return, alpha_return=alpha_return,
            benchmark="SPY", holding_days=holding_days, resolution_date=resolution_date,
        )
        return


async def _run_backtest(backtest_id: str) -> None:
    job = backtest_repository.get_backtest(backtest_id)
    if job is None:
        return
    backtest_repository.mark_backtest_running(backtest_id)
    semaphore = _get_semaphore()

    cells_by_symbol: dict[str, list[str]] = {}
    already_completed: set[tuple[str, str]] = set()
    for cell in job.cells:
        cells_by_symbol.setdefault(cell.symbol, []).append(cell.cell_date.isoformat())
        if cell.status == "COMPLETED":
            already_completed.add((cell.symbol, cell.cell_date.isoformat()))
    for dates in cells_by_symbol.values():
        dates.sort()  # chronological order required for upstream's own self-settlement (Section 20)

    from trading.ai_research.market.instruments import resolve_instrument
    from trading.ai_research.market.symbols import to_tradingagents_symbol

    market_value = job.market or "US"

    async def _sweep_symbol(symbol: str, dates: list[str]) -> None:
        provider_symbol = to_tradingagents_symbol(resolve_instrument(market_value, symbol))
        async with semaphore:
            for cell_date in dates:
                if backtest_repository.is_cancel_requested(backtest_id):
                    return
                # Resume (Section 20): never rerun an already-COMPLETED cell.
                if (symbol, cell_date) not in already_completed:
                    await _run_one_cell(job, symbol, cell_date)
            await _settle_symbol(job, provider_symbol)
            for cell_date in dates:
                _persist_evaluation_from_log(backtest_id, symbol, provider_symbol, cell_date)

    await asyncio.gather(*(_sweep_symbol(s, d) for s, d in cells_by_symbol.items()))

    if backtest_repository.is_cancel_requested(backtest_id):
        backtest_repository.mark_backtest_cancelled(backtest_id)
        return

    cells = backtest_repository.get_cells(backtest_id)
    if job.total_runs > 0 and sum(1 for c in cells if c.status == "COMPLETED") == 0:
        backtest_repository.mark_backtest_failed(backtest_id, "every research run in this backtest failed")
        return

    metrics = evaluator.compute_metrics(cells, holding_days=job.holding_days)
    backtest_repository.mark_backtest_completed(backtest_id, metrics)
    logger.info("ai_research.backtest.completed backtest_id=%s total=%d completed=%d failed=%d",
                backtest_id, job.total_runs, sum(1 for c in cells if c.status == "COMPLETED"), sum(1 for c in cells if c.status == "FAILED"))


def resume_backtest(backtest_id: str) -> tuple[AIBacktestJob, int]:
    """Section 20: continues only genuinely-unfinished cells (never
    reruns a COMPLETED one) and re-attempts evaluation settlement for
    cells stuck PENDING. Only a FAILED backtest (interrupted, or one
    where every run failed) may be resumed."""
    job = backtest_repository.get_backtest(backtest_id)
    if job is None:
        raise BacktestNotFoundError(backtest_id)
    if job.status not in ("FAILED", "CANCELLED"):
        raise BacktestActionNotAllowedError(f"only a FAILED or CANCELLED backtest may be resumed (current status: {job.status})")

    pending = backtest_repository.get_not_completed_cells(backtest_id)
    backtest_repository.clear_cancel_flag(backtest_id)
    asyncio.create_task(_run_backtest(backtest_id))
    return job, len(pending)


def cancel_backtest(backtest_id: str) -> AIBacktestJob:
    if not backtest_repository.request_cancel(backtest_id):
        job = backtest_repository.get_backtest(backtest_id)
        if job is None:
            raise BacktestNotFoundError(backtest_id)
        raise BacktestActionNotAllowedError(f"backtest is not in a cancellable state (current status: {job.status})")
    job = backtest_repository.get_backtest(backtest_id)
    return job


def get_backtest(backtest_id: str) -> AIBacktestJob | None:
    return backtest_repository.get_backtest(backtest_id)


def list_backtests(*, page: int = 1, page_size: int = 50, status: str | None = None):
    return backtest_repository.list_backtests(page=page, page_size=page_size, status=status)


def list_cells(backtest_id: str, *, page: int = 1, page_size: int = 50, symbol: str | None = None):
    return backtest_repository.list_cells_page(backtest_id, page=page, page_size=page_size, symbol=symbol)


def recover_interrupted_backtests() -> int:
    return backtest_repository.recover_interrupted_backtests()


def data_quality_matrix():
    from trading.ai_research.trading_agents_adapter import POINT_IN_TIME_SAFETY

    sources = []
    for name, info in POINT_IN_TIME_SAFETY.items():
        safe = info["safe"]
        label = "YES" if safe is True else ("NO" if safe is False else str(safe))
        sources.append({"source": name, "safe": label, "detail": info["detail"]})
    summary = (
        "Market data, news, sentiment, macro data, and decision-memory past-context "
        "are point-in-time safe. Company-profile fundamentals and live prediction-market "
        "odds are safe by REFUSAL (withheld entirely for historical dates, never leaked). "
        "Financial-statement fundamentals and insider transactions are PARTIALLY safe: "
        "filtered by period-end/transaction date, but the vendor reports no filing date, "
        "so a small filing-lag leak (days to weeks) is possible and cannot be ruled out. "
        "This backtest is NOT claimed to be fully unbiased for those two sources."
    )
    return {"sources": sources, "summary": summary}
