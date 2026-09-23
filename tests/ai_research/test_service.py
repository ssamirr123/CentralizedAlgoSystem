"""
AIResearchService tests -- background lifecycle, concurrency, timeout,
and failure-isolation. run_research() itself is monkeypatched (never
touches the real tradingagents package or an LLM) so these tests are
fast, deterministic, and cost nothing.

Phase 5: job state is database-backed (trading.ai_research.repository).
tests/conftest.py's autouse `_clean_db` fixture already gives every test
a fresh schema (AIResearchRun/AIResearchStage register on the same
canonical Base) -- no store fixture needed anymore.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from trading.ai_research import service
from trading.ai_research.schemas import ResearchJobStatus, ResearchRequestIn, ResearchStage
from trading.ai_research.trading_agents_adapter import (
    AIResearchConfigError,
    ResearchReport,
    ResearchResult,
)


@pytest.fixture(autouse=True)
def _fresh_semaphore(monkeypatch):
    service._reset_semaphore()
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    yield
    service._reset_semaphore()


def _request(**overrides) -> ResearchRequestIn:
    body = {"symbol": "AAPL", "research_date": date(2026, 1, 1).isoformat()}
    body.update(overrides)
    return ResearchRequestIn.model_validate(body)


def test_submit_when_disabled_raises(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "false")
    from trading.ai_research.trading_agents_adapter import AIResearchDisabledError

    with pytest.raises(AIResearchDisabledError):
        service.submit_research(_request())


def test_submit_returns_immediately_as_queued():
    async def _flow():
        job = service.submit_research(_request())
        assert job.status == ResearchJobStatus.QUEUED
        return job

    job = asyncio.run(_flow())
    assert job.research_id
    assert service.get_job(job.research_id) is not None


def test_full_lifecycle_completes(monkeypatch):
    fake_result = ResearchResult(
        instrument="AAPL", as_of_date="2026-01-01",
        report=ResearchReport(market_analysis="up", final_trade_decision="Hold", signal="Hold"),
        raw_state={},
    )
    monkeypatch.setattr("trading.ai_research.service.run_research", lambda *a, **k: fake_result)

    async def _flow():
        job = service.submit_research(_request())
        for _ in range(50):
            current = service.get_job(job.research_id)
            if current.status == ResearchJobStatus.COMPLETED:
                return current
            await asyncio.sleep(0.02)
        return service.get_job(job.research_id)

    final = asyncio.run(_flow())
    assert final.status == ResearchJobStatus.COMPLETED
    assert final.report.final_trade_decision == "Hold"
    assert final.report.signal == "Hold"
    assert final.started_at is not None
    assert final.completed_at is not None


def test_adapter_failure_marks_job_failed_not_crash(monkeypatch):
    def _boom(*a, **k):
        raise AIResearchConfigError("simulated provider failure")

    monkeypatch.setattr("trading.ai_research.service.run_research", _boom)

    async def _flow():
        job = service.submit_research(_request())
        for _ in range(50):
            current = service.get_job(job.research_id)
            if current.status == ResearchJobStatus.FAILED:
                return current
            await asyncio.sleep(0.02)
        return service.get_job(job.research_id)

    final = asyncio.run(_flow())
    assert final.status == ResearchJobStatus.FAILED
    assert "simulated provider failure" in final.error
    # No stack trace leakage: our sanitizer only stores str(exc), never a traceback object.
    assert "Traceback" not in final.error


def test_unexpected_exception_does_not_crash_and_marks_failed(monkeypatch):
    def _boom(*a, **k):
        raise ValueError("totally unexpected")

    monkeypatch.setattr("trading.ai_research.service.run_research", _boom)

    async def _flow():
        job = service.submit_research(_request())
        for _ in range(50):
            current = service.get_job(job.research_id)
            if current.status == ResearchJobStatus.FAILED:
                return current
            await asyncio.sleep(0.02)
        return service.get_job(job.research_id)

    final = asyncio.run(_flow())
    assert final.status == ResearchJobStatus.FAILED


def test_timeout_marks_job_failed(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_TIMEOUT_SECONDS", "1")

    def _slow(*a, **k):
        import time
        time.sleep(5)
        return ResearchResult(instrument="AAPL", as_of_date="2026-01-01", report=ResearchReport())

    monkeypatch.setattr("trading.ai_research.service.run_research", _slow)

    async def _flow():
        job = service.submit_research(_request())
        for _ in range(200):
            current = service.get_job(job.research_id)
            if current.status == ResearchJobStatus.FAILED:
                return current
            await asyncio.sleep(0.05)
        return service.get_job(job.research_id)

    final = asyncio.run(_flow())
    assert final.status == ResearchJobStatus.FAILED
    assert "timed out" in final.error
    assert final.provider_error == "TIMEOUT"


def test_stage_callback_updates_job_stage(monkeypatch):
    def _fake_run(*args, on_node_complete=None, **kwargs):
        if on_node_complete:
            on_node_complete("Market Analyst")
            on_node_complete("Aggressive Analyst")
        return ResearchResult(instrument="AAPL", as_of_date="2026-01-01", report=ResearchReport())

    monkeypatch.setattr("trading.ai_research.service.run_research", _fake_run)

    async def _flow():
        job = service.submit_research(_request())
        for _ in range(50):
            current = service.get_job(job.research_id)
            if current.status == ResearchJobStatus.COMPLETED:
                return current
            await asyncio.sleep(0.02)
        return service.get_job(job.research_id)

    final = asyncio.run(_flow())
    # Final stage always ends at COMPLETED regardless of intermediate stages seen.
    assert final.stage == ResearchStage.COMPLETED


def test_concurrency_limit_serializes_jobs(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_MAX_CONCURRENT", "1")
    service._reset_semaphore()

    running_count = {"current": 0, "max_seen": 0}

    def _tracked(*a, **k):
        import time
        running_count["current"] += 1
        running_count["max_seen"] = max(running_count["max_seen"], running_count["current"])
        time.sleep(0.1)
        running_count["current"] -= 1
        return ResearchResult(instrument="AAPL", as_of_date="2026-01-01", report=ResearchReport())

    monkeypatch.setattr("trading.ai_research.service.run_research", _tracked)

    async def _flow():
        job1 = service.submit_research(_request(symbol="AAPL"))
        job2 = service.submit_research(_request(symbol="MSFT"))
        for _ in range(100):
            j1 = service.get_job(job1.research_id)
            j2 = service.get_job(job2.research_id)
            if j1.status == ResearchJobStatus.COMPLETED and j2.status == ResearchJobStatus.COMPLETED:
                break
            await asyncio.sleep(0.02)

    asyncio.run(_flow())
    assert running_count["max_seen"] == 1, "AI_RESEARCH_MAX_CONCURRENT=1 must serialize research runs"


def test_history_lists_recent_jobs():
    async def _flow():
        service.submit_research(_request(symbol="AAPL"))
        service.submit_research(_request(symbol="MSFT"))
        await asyncio.sleep(0.05)

    asyncio.run(_flow())
    rows, total = service.list_history()
    symbols = {r.symbol for r in rows}
    assert {"AAPL", "MSFT"} <= symbols
    assert total >= 2
