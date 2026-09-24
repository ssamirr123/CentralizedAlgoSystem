"""
Phase 3/5 tests: real per-node stage progress (repository.py, DB-backed
since Phase 5), retry/vendor config validation, decision-memory read API,
and checkpoint resume. No real LLM/network call anywhere in this file.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trading.ai_research import repository
from trading.ai_research.schemas import (
    NodeStageStatus,
    ResearchJobStatus,
    ResearchRequestIn,
)
from trading.ai_research.trading_agents_adapter import (
    RETRY_COUNT_MAX,
    real_node_order,
    stage_bucket_for_node,
)


# --------------------------------------------------------------------------- #
# real_node_order / stage_bucket_for_node -- grounded in real upstream names
# --------------------------------------------------------------------------- #
def test_real_node_order_all_analysts():
    order = real_node_order(("market", "social", "news", "fundamentals"))
    assert order == (
        "Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst",
        "Bull Researcher", "Bear Researcher", "Research Manager",
        "Trader", "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
        "Portfolio Manager",
    )


def test_real_node_order_subset_analysts_omits_unselected():
    """Section 7: an unselected analyst is OMITTED entirely, not shown as
    perpetually PENDING or fabricated as SKIPPED."""
    order = real_node_order(("market", "news"))
    assert "Sentiment Analyst" not in order
    assert "Fundamentals Analyst" not in order
    assert order[:2] == ("Market Analyst", "News Analyst")


@pytest.mark.parametrize("node,bucket", [
    ("Market Analyst", "ANALYSTS"), ("Fundamentals Analyst", "ANALYSTS"),
    ("Bull Researcher", "RESEARCH_DEBATE"), ("Research Manager", "RESEARCH_DEBATE"),
    ("Trader", "TRADER"), ("Aggressive Analyst", "RISK"), ("Portfolio Manager", "PORTFOLIO_MANAGER"),
])
def test_stage_bucket_mapping_uses_real_names(node, bucket):
    assert stage_bucket_for_node(node) == bucket


def test_unrecognized_node_has_no_bucket():
    assert stage_bucket_for_node("tools_market") is None  # internal plumbing node, not a stage


# --------------------------------------------------------------------------- #
# repository.py: real per-node progress inference (Phase 5: DB-backed --
# uses the same test DB tests/conftest.py's autouse _clean_db fixture
# already creates/drops for every test, since AIResearchRun/AIResearchStage
# register on the same canonical Base).
# --------------------------------------------------------------------------- #
def _job(analysts=("market", "social", "news", "fundamentals")):
    req = ResearchRequestIn.model_validate(_base(selected_analysts=list(analysts)))
    job = repository.create_run(req)
    repository.mark_running(job.research_id)
    return job.research_id


def test_first_node_running_on_start():
    rid = _job()
    job = repository.get_run(rid)
    assert job.node_stages[0].name == "Market Analyst"
    assert job.node_stages[0].status == NodeStageStatus.RUNNING
    assert job.current_stage == "Market Analyst"
    assert all(s.status == NodeStageStatus.PENDING for s in job.node_stages[1:])


def test_node_complete_advances_to_next():
    rid = _job()
    repository.mark_node_complete(rid, "Market Analyst")
    job = repository.get_run(rid)
    assert job.node_stages[0].status == NodeStageStatus.COMPLETED
    assert job.node_stages[0].duration_ms is not None
    assert job.node_stages[1].name == "Sentiment Analyst"
    assert job.node_stages[1].status == NodeStageStatus.RUNNING
    assert job.current_stage == "Sentiment Analyst"


def test_repeated_node_completion_across_debate_rounds():
    """Bull Researcher can legitimately complete multiple times (once per
    debate round) -- the second occurrence must not error or double-count
    against an already-COMPLETED entry; it's simply ignored once no
    PENDING/RUNNING slot remains for that name."""
    rid = _job(analysts=("market",))
    for node in ("Market Analyst", "Bull Researcher"):
        repository.mark_node_complete(rid, node)
    # Second "Bull Researcher" completion (round 2) -- must not raise.
    repository.mark_node_complete(rid, "Bull Researcher")
    job = repository.get_run(rid)
    bull = next(s for s in job.node_stages if s.name == "Bull Researcher")
    assert bull.status == NodeStageStatus.COMPLETED


def test_unrecognized_node_name_ignored_not_error():
    rid = _job()
    repository.mark_node_complete(rid, "tools_market")  # internal node, not in the plan
    job = repository.get_run(rid)
    assert job.node_stages[0].status == NodeStageStatus.RUNNING  # unaffected


def test_failure_does_not_fabricate_downstream_completion():
    """Section 8: on failure, the RUNNING node becomes FAILED; everything
    after it stays PENDING -- never fabricated as completed."""
    rid = _job()
    repository.mark_node_complete(rid, "Market Analyst")  # first node done
    repository.mark_failed(rid, "simulated failure")
    job = repository.get_run(rid)
    assert job.node_stages[0].status == NodeStageStatus.COMPLETED  # stays completed
    assert job.node_stages[1].status == NodeStageStatus.FAILED     # was RUNNING -> FAILED
    assert all(s.status == NodeStageStatus.PENDING for s in job.node_stages[2:])  # not fabricated


def test_completion_finalizes_any_stray_pending_node():
    """If the graph's own conditional edges mean a node never produced a
    distinct completion event before the job overall completed, it's
    marked COMPLETED at that point rather than left stale."""
    rid = _job(analysts=("market",))
    from trading.ai_research.schemas import ResearchReportOut

    repository.mark_completed(rid, ResearchReportOut())
    job = repository.get_run(rid)
    assert all(s.status == NodeStageStatus.COMPLETED for s in job.node_stages)


# --------------------------------------------------------------------------- #
# Retry / data-vendor validation (Section 12/13)
# --------------------------------------------------------------------------- #
def _base(**overrides):
    body = {"symbol": "AAPL", "research_date": "2026-01-01"}
    body.update(overrides)
    return body


def test_retry_count_within_bounds_accepted():
    req = ResearchRequestIn.model_validate(_base(retry_count=3))
    assert req.retry_count == 3


def test_retry_count_above_max_rejected():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(retry_count=RETRY_COUNT_MAX + 1))


def test_retry_count_negative_rejected():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(retry_count=-1))


def test_valid_data_vendor_accepted():
    req = ResearchRequestIn.model_validate(_base(data_vendors={"core_stock_apis": "alpha_vantage"}))
    assert req.data_vendors["core_stock_apis"] == "alpha_vantage"


def test_unknown_data_vendor_category_rejected():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(data_vendors={"not_a_real_category": "yfinance"}))


def test_unsupported_vendor_for_category_rejected():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(data_vendors={"macro_data": "yfinance"}))  # macro_data only supports fred


# --------------------------------------------------------------------------- #
# Decision memory: safe, path-traversal-proof read API
# --------------------------------------------------------------------------- #
def test_memory_empty_when_no_log_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_RESEARCH_DATA_DIR", str(tmp_path))
    from trading.ai_research.trading_agents_adapter import list_memory_entries

    assert list_memory_entries() == []


def test_memory_path_traversal_id_finds_nothing(monkeypatch, tmp_path):
    """memory_id is a logical key looked up against parsed entries, never
    a filesystem path -- a traversal-shaped id simply matches no entry."""
    monkeypatch.setenv("AI_RESEARCH_DATA_DIR", str(tmp_path))
    from trading.ai_research.trading_agents_adapter import get_memory_entry

    assert get_memory_entry("../../../etc/passwd") is None
    assert get_memory_entry("..\\..\\windows\\system32\\config\\SAM") is None


def test_memory_reads_real_written_entry(monkeypatch, tmp_path):
    """Writes via TradingMemoryLog's own real API (as run_research()
    would, through the SAME controlled path), then reads it back through
    our adapter's list/get functions -- proves the read side actually
    parses upstream's own real log format, not a guessed one.

    NOTE (Section 18 in action): a bare `pytest.importorskip("tradingagents")`
    is NOT sufficient here -- this dev machine has an unrelated PyPI
    impostor package also named "tradingagents" on its global path (see
    Phase 1/2/3 reports), which imports successfully but has an
    incompatible decision-log entry schema. `tradingagents.portfolio` is
    a module the genuine TauricResearch package has and the impostor does
    not (confirmed in Phase 2), so it's used here as the real
    disambiguating skip condition instead.
    """
    pytest.importorskip(
        "tradingagents.portfolio",
        reason="requires the genuine TauricResearch/TradingAgents package (isolated venv only) -- "
               "not the unrelated PyPI package of the same name",
    )
    monkeypatch.setenv("AI_RESEARCH_DATA_DIR", str(tmp_path))
    from tradingagents.agents.utils.memory import TradingMemoryLog

    log_path = tmp_path / "memory" / "trading_memory.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = TradingMemoryLog({"memory_log_path": str(log_path)})
    log.store_decision(ticker="AAPL", trade_date="2026-01-01", final_trade_decision="Rating: Hold")

    from trading.ai_research.trading_agents_adapter import get_memory_entry, list_memory_entries

    entries = list_memory_entries()
    assert len(entries) == 1
    assert entries[0].ticker == "AAPL"
    assert entries[0].memory_id == "2026-01-01:AAPL"

    fetched = get_memory_entry("2026-01-01:AAPL")
    assert fetched is not None
    assert fetched.decision == "Rating: Hold"


# --------------------------------------------------------------------------- #
# Checkpoint / resume (Section 15)
# --------------------------------------------------------------------------- #
def test_resume_rejects_unknown_research_id():
    from trading.ai_research import service

    with pytest.raises(service.ResumeNotAllowedError):
        service.resume_research("does-not-exist")


def test_resume_rejects_non_failed_job(monkeypatch):
    import asyncio

    from trading.ai_research import service

    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    service._reset_semaphore()

    async def _flow():
        job = service.submit_research(ResearchRequestIn.model_validate(_base()))
        with pytest.raises(service.ResumeNotAllowedError):
            service.resume_research(job.research_id)  # still QUEUED/RUNNING

    asyncio.run(_flow())
    service._reset_semaphore()


def test_resume_reports_checkpoint_flag_honestly(monkeypatch):
    """A resumed job with checkpoint_enabled=False must report
    resumed_from_checkpoint=False -- Phase 3 explicitly forbids
    "inventing" resume behavior upstream doesn't actually provide."""
    import asyncio

    from trading.ai_research import service
    from trading.ai_research.trading_agents_adapter import AIResearchConfigError

    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    service._reset_semaphore()

    def _boom(*a, **k):
        raise AIResearchConfigError("simulated")

    monkeypatch.setattr("trading.ai_research.service.run_research", _boom)

    async def _flow():
        req = ResearchRequestIn.model_validate(_base(checkpoint_enabled=False))
        job = service.submit_research(req)
        for _ in range(50):
            current = service.get_job(job.research_id)
            if current.status == ResearchJobStatus.FAILED:
                break
            await asyncio.sleep(0.02)
        new_job, resumed_from_checkpoint = service.resume_research(job.research_id)
        assert resumed_from_checkpoint is False
        assert new_job.research_id != job.research_id

    asyncio.run(_flow())
    service._reset_semaphore()


def test_resume_works_after_simulated_restart(monkeypatch):
    """Section 15: resume must work purely from the DATABASE row, with no
    in-memory request object surviving -- simulated here by never keeping
    a reference to the original ResearchJob beyond its research_id.

    run_research is mocked (as test_resume_reports_checkpoint_flag_honestly
    does above) so the scheduled background task can't race ahead to
    RUNNING before this test's assertion runs -- asyncio.run() only
    guarantees _flow() itself completes, not that the task submit_research()
    schedules via create_task() hasn't already gotten a turn (it reliably
    does, since resume_research -> submit_research -> the DB write inside
    _run_job's asyncio.to_thread call yields control back to the loop)."""
    import asyncio

    from trading.ai_research import service
    from trading.ai_research.trading_agents_adapter import AIResearchConfigError

    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    service._reset_semaphore()

    def _boom(*a, **k):
        raise AIResearchConfigError("simulated")

    monkeypatch.setattr("trading.ai_research.service.run_research", _boom)

    req = ResearchRequestIn.model_validate(_base(checkpoint_enabled=True))
    job = repository.create_run(req)
    repository.mark_failed(job.research_id, "simulated failure")

    async def _flow():
        result = service.resume_research(job.research_id)
        await asyncio.sleep(0.05)  # let the background task settle to its terminal state
        return result

    new_job, resumed_from_checkpoint = asyncio.run(_flow())
    assert resumed_from_checkpoint is True
    assert new_job.symbol == "AAPL"
    reloaded = repository.get_run(new_job.research_id)
    assert reloaded is not None
    # The DB row -- not an in-memory object -- proves the resume was
    # persisted and driven to completion purely from what was written to
    # the database (the original request row), fulfilling Section 15.
    assert reloaded.status == ResearchJobStatus.FAILED
    service._reset_semaphore()


# --------------------------------------------------------------------------- #
# Provider-error classification (Section 16)
# --------------------------------------------------------------------------- #
def test_rate_limit_classified_not_as_application_defect():
    from trading.ai_research.trading_agents_adapter import _classify_provider_error

    exc = RuntimeError("Error code: 413 - Request too large ... tokens per minute (TPM) ... rate_limit_exceeded")
    assert _classify_provider_error(exc) == "RATE_LIMIT"


def test_auth_error_classified():
    from trading.ai_research.trading_agents_adapter import _classify_provider_error

    exc = RuntimeError("401 Unauthorized: invalid api key")
    assert _classify_provider_error(exc) == "AUTH_ERROR"


def test_unrecognized_error_not_misclassified():
    from trading.ai_research.trading_agents_adapter import _classify_provider_error

    exc = RuntimeError("something completely unrelated happened")
    assert _classify_provider_error(exc) is None
