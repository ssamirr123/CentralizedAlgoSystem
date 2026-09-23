"""
AIBacktestService tests -- run_research()/settle_pending_decisions() are
monkeypatched (never touch the real tradingagents package or an LLM), same
discipline as tests/ai_research/test_service.py.
"""
from __future__ import annotations

import asyncio

import pytest

from trading.ai_research.backtest import repository, service
from trading.ai_research.backtest.schemas import BacktestRequestIn
from trading.ai_research.trading_agents_adapter import AIResearchConfigError, ResearchReport, ResearchResult


@pytest.fixture(autouse=True)
def _fresh_semaphore(monkeypatch):
    service._reset_semaphore()
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    yield
    service._reset_semaphore()


def _request(**overrides) -> BacktestRequestIn:
    body = {"symbols": ["AAPL"], "start_date": "2026-01-01", "end_date": "2026-01-08"}
    body.update(overrides)
    return BacktestRequestIn.model_validate(body)


def _fake_result(decision: str = "Hold", signal: str = "Hold") -> ResearchResult:
    return ResearchResult(
        instrument="AAPL", as_of_date="2026-01-01",
        report=ResearchReport(final_trade_decision=decision, signal=signal),
        raw_state={},
    )


async def _wait_for(predicate, attempts=100, delay=0.02):
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return predicate()


# --------------------------------------------------------------------------- #
# Cost/workload guard (Section 7)
# --------------------------------------------------------------------------- #
def test_submit_when_disabled_raises(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "false")
    from trading.ai_research.trading_agents_adapter import AIResearchDisabledError

    with pytest.raises(AIResearchDisabledError):
        service.submit_backtest(_request())


def test_max_symbols_exceeded_rejects_request(monkeypatch):
    monkeypatch.setenv("AI_BACKTEST_MAX_SYMBOLS", "1")
    with pytest.raises(service.BacktestLimitExceededError, match="symbols"):
        service.submit_backtest(_request(symbols=["AAPL", "MSFT"]))


def test_max_dates_exceeded_rejects_request(monkeypatch):
    monkeypatch.setenv("AI_BACKTEST_MAX_DATES", "1")
    with pytest.raises(service.BacktestLimitExceededError, match="dates"):
        service.submit_backtest(_request(start_date="2026-01-01", end_date="2026-02-01", frequency="weekly"))


def test_max_runs_exceeded_rejects_request(monkeypatch):
    monkeypatch.setenv("AI_BACKTEST_MAX_SYMBOLS", "5")
    monkeypatch.setenv("AI_BACKTEST_MAX_DATES", "5")
    monkeypatch.setenv("AI_BACKTEST_MAX_RUNS", "2")
    with pytest.raises(service.BacktestLimitExceededError, match="research runs"):
        service.submit_backtest(_request(symbols=["AAPL", "MSFT"], start_date="2026-01-01", end_date="2026-01-15", frequency="weekly"))


def test_limit_violation_never_truncates_just_rejects(monkeypatch):
    """A rejected request must create NOTHING -- never a silently truncated job."""
    monkeypatch.setenv("AI_BACKTEST_MAX_SYMBOLS", "1")
    with pytest.raises(service.BacktestLimitExceededError):
        service.submit_backtest(_request(symbols=["AAPL", "MSFT"]))
    rows, total = repository.list_backtests()
    assert total == 0


def test_estimate_matches_actual_grid_and_never_creates_a_job():
    est = service.estimate(_request(start_date="2026-01-01", end_date="2026-01-15", frequency="weekly"))
    assert est.total_runs == est.symbol_count * est.date_count
    assert est.exceeds_limit is False
    rows, total = repository.list_backtests()
    assert total == 0


def test_estimate_flags_exceeding_configured_limit(monkeypatch):
    monkeypatch.setenv("AI_BACKTEST_MAX_RUNS", "1")
    est = service.estimate(_request(start_date="2026-01-01", end_date="2026-01-15", frequency="weekly"))
    assert est.exceeds_limit is True
    assert est.limit_message is not None


# --------------------------------------------------------------------------- #
# Lifecycle / progress (Section 8/9)
# --------------------------------------------------------------------------- #
def test_full_lifecycle_completes(monkeypatch):
    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", lambda *a, **k: _fake_result())
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    async def _flow():
        job = service.submit_backtest(_request(start_date="2026-01-01", end_date="2026-01-01"))
        await _wait_for(lambda: repository.get_backtest(job.backtest_id).status in ("COMPLETED", "FAILED"))
        return repository.get_backtest(job.backtest_id)

    final = asyncio.run(_flow())
    assert final.status == "COMPLETED"
    assert final.completed_runs == 1
    assert final.failed_runs == 0
    assert final.metrics is not None
    assert final.metrics["label"] == "AI Research Decision Evaluation"


def test_progress_derives_only_from_real_run_counts(monkeypatch):
    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", lambda *a, **k: _fake_result())
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    async def _flow():
        job = service.submit_backtest(_request(start_date="2026-01-01", end_date="2026-01-15", frequency="weekly"))
        await _wait_for(lambda: repository.get_backtest(job.backtest_id).status in ("COMPLETED", "FAILED"))
        return repository.get_backtest(job.backtest_id)

    final = asyncio.run(_flow())
    assert final.total_runs == final.completed_runs + final.failed_runs


# --------------------------------------------------------------------------- #
# Failure isolation (Section 21)
# --------------------------------------------------------------------------- #
def test_one_failed_cell_does_not_abort_the_whole_backtest(monkeypatch):
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AIResearchConfigError("simulated single-cell failure")
        return _fake_result()

    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", _flaky)
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    async def _flow():
        job = service.submit_backtest(_request(start_date="2026-01-01", end_date="2026-01-15", frequency="weekly"))
        await _wait_for(lambda: repository.get_backtest(job.backtest_id).status in ("COMPLETED", "FAILED"))
        return repository.get_backtest(job.backtest_id)

    final = asyncio.run(_flow())
    assert final.failed_runs == 1
    assert final.completed_runs >= 1
    assert final.status == "COMPLETED", "at least one cell succeeded -- must not be reported as a total failure"


def test_all_cells_failing_marks_backtest_failed_not_completed(monkeypatch):
    def _always_boom(*a, **k):
        raise AIResearchConfigError("simulated: every cell fails")

    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", _always_boom)
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    async def _flow():
        job = service.submit_backtest(_request(start_date="2026-01-01", end_date="2026-01-01"))
        await _wait_for(lambda: repository.get_backtest(job.backtest_id).status in ("COMPLETED", "FAILED"))
        return repository.get_backtest(job.backtest_id)

    final = asyncio.run(_flow())
    assert final.status == "FAILED"
    assert final.metrics is None


# --------------------------------------------------------------------------- #
# Decision normalization traceability (Section 12/18)
# --------------------------------------------------------------------------- #
def test_completed_cell_links_to_a_real_research_run(monkeypatch):
    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", lambda *a, **k: _fake_result("Rating: Buy", "Buy"))
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)
    monkeypatch.setattr(
        "trading.ai_research.trading_agents_adapter.get_memory_entry",
        lambda memory_id: __import__("trading.ai_research.trading_agents_adapter", fromlist=["MemoryEntry"]).MemoryEntry(
            memory_id=memory_id, ticker="AAPL", trade_date="2026-01-01", rating="Buy",
            pending=True, decision="Rating: Buy", reflection=None,
        ),
    )

    async def _flow():
        job = service.submit_backtest(_request(start_date="2026-01-01", end_date="2026-01-01"))
        await _wait_for(lambda: repository.get_backtest(job.backtest_id).status in ("COMPLETED", "FAILED"))
        return job.backtest_id

    backtest_id = asyncio.run(_flow())
    cell = repository.get_cells(backtest_id)[0]
    assert cell.research_id is not None
    from trading.ai_research import repository as research_repository

    research_row = research_repository.get_run(cell.research_id)
    assert research_row is not None
    assert research_row.status.value == "COMPLETED"
    assert cell.normalized_decision == "Buy"


# --------------------------------------------------------------------------- #
# Concurrency (Section 9/30)
# --------------------------------------------------------------------------- #
def test_max_concurrent_limits_simultaneous_symbol_sweeps(monkeypatch):
    monkeypatch.setenv("AI_BACKTEST_MAX_CONCURRENT", "1")
    service._reset_semaphore()

    running = {"current": 0, "max_seen": 0}

    def _tracked(*a, **k):
        import time
        running["current"] += 1
        running["max_seen"] = max(running["max_seen"], running["current"])
        time.sleep(0.05)
        running["current"] -= 1
        return _fake_result()

    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", _tracked)
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    async def _flow():
        job = service.submit_backtest(_request(symbols=["AAPL", "MSFT"], start_date="2026-01-01", end_date="2026-01-01"))
        await _wait_for(lambda: repository.get_backtest(job.backtest_id).status in ("COMPLETED", "FAILED"), attempts=200)

    asyncio.run(_flow())
    assert running["max_seen"] == 1, "AI_BACKTEST_MAX_CONCURRENT=1 must serialize symbol sweeps"


# --------------------------------------------------------------------------- #
# Cancel (Section 22)
# --------------------------------------------------------------------------- #
def test_cancel_stops_before_the_next_cell(monkeypatch):
    def _slow(*a, **k):
        import time
        time.sleep(0.05)
        return _fake_result()

    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", _slow)
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    async def _flow():
        job = service.submit_backtest(_request(start_date="2026-01-01", end_date="2026-01-15", frequency="weekly"))
        service.cancel_backtest(job.backtest_id)
        await _wait_for(lambda: repository.get_backtest(job.backtest_id).status in ("COMPLETED", "FAILED", "CANCELLED"))
        return repository.get_backtest(job.backtest_id)

    final = asyncio.run(_flow())
    assert final.status == "CANCELLED"
    assert final.completed_runs < final.total_runs, "cancel must not let every cell finish"


def test_cancel_rejected_for_unknown_backtest():
    with pytest.raises(service.BacktestNotFoundError):
        service.cancel_backtest("does-not-exist")


# --------------------------------------------------------------------------- #
# Resume (Section 20)
# --------------------------------------------------------------------------- #
def test_resume_only_reruns_unfinished_cells(monkeypatch):
    calls = {"symbols_dates": []}

    def _record(instrument, as_of_date, *a, **k):
        calls["symbols_dates"].append((instrument, as_of_date))
        return _fake_result()

    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", _record)
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    backtest_id = repository.create_backtest(
        _request(start_date="2026-01-01", end_date="2026-01-08"), ("2026-01-01", "2026-01-08"), holding_days=5,
    )
    repository.mark_cell_completed(backtest_id, "AAPL", "2026-01-01", research_id="r-existing", raw_decision="Hold", normalized_decision="Hold")
    repository.mark_backtest_failed(backtest_id, "simulated interruption")

    async def _flow():
        job, pending_count = service.resume_backtest(backtest_id)
        assert pending_count == 1
        await asyncio.sleep(0.05)  # let the newly-scheduled task get its first turn before polling
        await _wait_for(lambda: repository.get_backtest(backtest_id).status in ("COMPLETED", "FAILED"))

    asyncio.run(_flow())
    assert calls["symbols_dates"] == [("AAPL", "2026-01-08")], "resume must not re-run the already-COMPLETED cell"
    final = repository.get_backtest(backtest_id)
    assert final.completed_runs == 2


def test_resume_rejected_for_running_backtest(monkeypatch):
    backtest_id = repository.create_backtest(_request(), ("2026-01-01",), holding_days=5)
    repository.mark_backtest_running(backtest_id)
    with pytest.raises(service.BacktestActionNotAllowedError):
        service.resume_backtest(backtest_id)


def test_resume_rejected_for_unknown_backtest():
    with pytest.raises(service.BacktestNotFoundError):
        service.resume_backtest("does-not-exist")


# --------------------------------------------------------------------------- #
# Data quality / bias disclosure (Section 28)
# --------------------------------------------------------------------------- #
def test_data_quality_matrix_flags_partial_sources_honestly():
    matrix = service.data_quality_matrix()
    sources = {s["source"]: s["safe"] for s in matrix["sources"]}
    assert sources["market_data"] == "YES"
    assert sources["fundamentals_financial_statements"] == "PARTIAL"
    assert sources["prediction_markets"] == "WITHHELD"
    assert "not claimed to be fully unbiased" in matrix["summary"].lower()
