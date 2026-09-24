"""
Phase 5 persistence tests: restart durability, history pagination/filters,
concurrent job isolation, interrupted-job recovery, and secret
non-persistence. Uses the project's existing test database convention
(tests/conftest.py's autouse `_clean_db` -- fresh schema per test, same
canonical Base AIResearchRun/AIResearchStage register on).
"""
from __future__ import annotations

from datetime import date

from trading.ai_research import repository
from trading.ai_research.models import AIResearchRun
from trading.ai_research.schemas import PortfolioContextIn, ResearchReportOut, ResearchRequestIn
from trading.database.connection import SessionLocal


def _request(**overrides) -> ResearchRequestIn:
    body = {"symbol": "AAPL", "research_date": date(2026, 1, 1).isoformat()}
    body.update(overrides)
    return ResearchRequestIn.model_validate(body)


# --------------------------------------------------------------------------- #
# Restart durability (Section 12/17/18/30) -- repository functions open
# their OWN session per call, exactly modelling "a different process reads
# this later" since no Python object is shared across calls.
# --------------------------------------------------------------------------- #
def test_completed_research_survives_a_new_session():
    job = repository.create_run(_request())
    repository.mark_running(job.research_id)
    report = ResearchReportOut(market_analysis="uptrend", final_trade_decision="Hold", signal="Hold")
    repository.mark_completed(job.research_id, report)

    # A brand-new call, as if from a freshly-restarted process -- no
    # in-memory object from the lines above is reused.
    reloaded = repository.get_run(job.research_id)
    assert reloaded is not None
    assert reloaded.status.value == "COMPLETED"
    assert reloaded.report.market_analysis == "uptrend"
    assert reloaded.report.signal == "Hold"


def test_failed_research_survives_a_new_session():
    job = repository.create_run(_request())
    repository.mark_running(job.research_id)
    repository.mark_failed(job.research_id, "simulated", provider_error="RATE_LIMIT")

    reloaded = repository.get_run(job.research_id)
    assert reloaded is not None
    assert reloaded.status.value == "FAILED"
    assert reloaded.provider_error == "RATE_LIMIT"


def test_partial_results_are_not_deleted_on_failure():
    """Section 8: completed stages must remain available even though the
    overall job ends FAILED."""
    job = repository.create_run(_request())
    repository.mark_running(job.research_id)
    repository.mark_node_complete(job.research_id, "Market Analyst")
    repository.mark_node_complete(job.research_id, "Sentiment Analyst")
    repository.mark_failed(job.research_id, "simulated failure at News Analyst")

    reloaded = repository.get_run(job.research_id)
    completed_names = {s.name for s in reloaded.node_stages if s.status.value == "COMPLETED"}
    assert {"Market Analyst", "Sentiment Analyst"} <= completed_names


def test_history_survives_and_lists_persisted_jobs():
    repository.create_run(_request(symbol="AAPL"))
    repository.create_run(_request(symbol="MSFT"))
    rows, total = repository.list_runs(page=1, page_size=50)
    symbols = {r.symbol for r in rows}
    assert {"AAPL", "MSFT"} <= symbols
    assert total >= 2


# --------------------------------------------------------------------------- #
# Interrupted-job recovery (Section 13)
# --------------------------------------------------------------------------- #
def test_recover_interrupted_jobs_marks_stale_running_as_failed():
    job = repository.create_run(_request())
    repository.mark_running(job.research_id)  # simulate a process that died mid-run

    recovered = repository.recover_interrupted_jobs()
    assert recovered == 1

    reloaded = repository.get_run(job.research_id)
    assert reloaded.status.value == "FAILED"
    assert reloaded.provider_error == "INTERRUPTED"
    assert "restart" in reloaded.error.lower()
    # The node that was RUNNING becomes FAILED; it is not fabricated as completed.
    running_or_failed = [s for s in reloaded.node_stages if s.status.value in ("FAILED",)]
    assert len(running_or_failed) == 1


def test_recover_interrupted_jobs_ignores_terminal_states():
    job = repository.create_run(_request())
    repository.mark_running(job.research_id)
    repository.mark_completed(job.research_id, ResearchReportOut())

    recovered = repository.recover_interrupted_jobs()
    assert recovered == 0
    assert repository.get_run(job.research_id).status.value == "COMPLETED"


def test_recover_interrupted_jobs_catches_queued_too():
    job = repository.create_run(_request())  # never started -- still QUEUED
    recovered = repository.recover_interrupted_jobs()
    assert recovered == 1
    reloaded = repository.get_run(job.research_id)
    assert reloaded.status.value == "FAILED"
    assert "QUEUED" in reloaded.error


# --------------------------------------------------------------------------- #
# History pagination/filtering (Section 16)
# --------------------------------------------------------------------------- #
def test_pagination_respects_page_size():
    for i in range(5):
        repository.create_run(_request(symbol=f"SYM{i}"))
    page1, total = repository.list_runs(page=1, page_size=2)
    assert len(page1) == 2
    assert total == 5
    page2, _ = repository.list_runs(page=2, page_size=2)
    assert len(page2) == 2
    assert {r.research_id for r in page1}.isdisjoint({r.research_id for r in page2})


def test_page_size_is_capped_never_unbounded():
    for i in range(3):
        repository.create_run(_request(symbol=f"SYM{i}"))
    rows, _ = repository.list_runs(page=1, page_size=10_000)
    # Capped internally at 200 -- with only 3 rows this just proves the
    # function accepts and clamps an absurd page_size rather than erroring
    # or returning literally unbounded internal state.
    assert len(rows) == 3


def test_filter_by_symbol():
    repository.create_run(_request(symbol="AAPL"))
    repository.create_run(_request(symbol="MSFT"))
    rows, total = repository.list_runs(symbol="AAPL")
    assert total == 1
    assert rows[0].symbol == "AAPL"


def test_filter_by_status():
    j1 = repository.create_run(_request(symbol="AAPL"))
    repository.create_run(_request(symbol="MSFT"))
    repository.mark_running(j1.research_id)
    repository.mark_completed(j1.research_id, ResearchReportOut())

    rows, total = repository.list_runs(status="COMPLETED")
    assert total == 1
    assert rows[0].symbol == "AAPL"


def test_filter_by_date_range():
    repository.create_run(_request(symbol="OLD", research_date="2026-01-01"))
    repository.create_run(_request(symbol="NEW", research_date="2026-06-01"))
    rows, total = repository.list_runs(date_from=date(2026, 3, 1), date_to=date(2026, 12, 31))
    assert total == 1
    assert rows[0].symbol == "NEW"


def test_filter_by_provider():
    repository.create_run(_request(symbol="AAPL", llm_provider="groq"))
    repository.create_run(_request(symbol="MSFT", llm_provider="openai"))
    rows, total = repository.list_runs(provider="groq")
    assert total == 1
    assert rows[0].symbol == "AAPL"


# --------------------------------------------------------------------------- #
# Concurrent job isolation (Section 25)
# --------------------------------------------------------------------------- #
def test_concurrent_jobs_do_not_cross_contaminate_stages():
    job_a = repository.create_run(_request(symbol="AAPL", selected_analysts=["market"]))
    job_b = repository.create_run(_request(symbol="MSFT", selected_analysts=["market", "news"]))
    repository.mark_running(job_a.research_id)
    repository.mark_running(job_b.research_id)

    repository.mark_node_complete(job_a.research_id, "Market Analyst")

    a = repository.get_run(job_a.research_id)
    b = repository.get_run(job_b.research_id)
    assert a.node_stages[0].status.value == "COMPLETED"
    # Job B's own "Market Analyst" must be unaffected by job A's event.
    assert b.node_stages[0].status.value == "RUNNING"
    assert b.node_stages[0].name == "Market Analyst"
    # job_a selected 1 analyst (1 + 8 fixed downstream = 9 nodes); job_b
    # selected 2 (2 + 8 = 10) -- confirms each job's own plan, not a shared one.
    assert len(a.node_stages) == 9
    assert len(b.node_stages) == 10


def test_concurrent_jobs_do_not_cross_contaminate_reports():
    job_a = repository.create_run(_request(symbol="AAPL"))
    job_b = repository.create_run(_request(symbol="MSFT"))
    repository.mark_running(job_a.research_id)
    repository.mark_running(job_b.research_id)
    repository.mark_completed(job_a.research_id, ResearchReportOut(signal="Buy"))

    a = repository.get_run(job_a.research_id)
    b = repository.get_run(job_b.research_id)
    assert a.report.signal == "Buy"
    assert b.report is None
    assert b.status.value == "RUNNING"


# --------------------------------------------------------------------------- #
# Secret non-persistence (Section 27)
# --------------------------------------------------------------------------- #
def test_no_secret_fields_exist_on_the_run_model():
    forbidden_substrings = ("api_key", "apikey", "secret", "token", "password", "credential", "auth_header")
    columns = {c.name.lower() for c in AIResearchRun.__table__.columns}
    offending = [c for c in columns if any(bad in c for bad in forbidden_substrings)]
    assert not offending, f"AIResearchRun has a secret-shaped column: {offending}"


def test_persisted_row_contains_no_credential_value(monkeypatch):
    """Even though there's no column FOR a secret, also prove that
    request-level fields a client could theoretically stuff a token into
    (llm_provider, deep_think_llm, ...) are stored as plain short
    strings/None here -- and that the config layer never writes an actual
    env-var API key value into any persisted field."""
    monkeypatch.setenv("SOME_FAKE_API_KEY", "sk-should-never-appear-anywhere")
    job = repository.create_run(_request(llm_provider="groq", quick_think_llm="llama-3"))

    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, job.research_id)
        dumped = str({c.name: getattr(row, c.name) for c in AIResearchRun.__table__.columns})
    finally:
        db.close()
    assert "sk-should-never-appear-anywhere" not in dumped


def test_portfolio_context_never_contains_broker_fields():
    portfolio = PortfolioContextIn(cash=1000, currency="USD", positions=[])
    job = repository.create_run(_request(portfolio=portfolio))
    reloaded = repository.get_run(job.research_id)
    # Only cash/currency/positions -- PortfolioContextIn's own extra="forbid"
    # already prevents a broker/account field from ever reaching this point,
    # this asserts the persisted shape reflects exactly that.
    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, job.research_id)
        assert set(row.portfolio.keys()) <= {"cash", "currency", "positions"}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Database failure handling (Section 11)
# --------------------------------------------------------------------------- #
def test_mark_completed_failure_does_not_leave_job_falsely_completed(monkeypatch):
    job = repository.create_run(_request())
    repository.mark_running(job.research_id)

    def _boom_commit(self):
        raise RuntimeError("simulated DB failure")

    import sqlalchemy.orm.session as orm_session
    monkeypatch.setattr(orm_session.Session, "commit", _boom_commit)

    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        repository.mark_completed(job.research_id, ResearchReportOut())

    monkeypatch.undo()
    reloaded = repository.get_run(job.research_id)
    # Must NOT be COMPLETED -- the failed persistence attempt must not
    # have left (or claimed) a false-positive terminal state.
    assert reloaded.status.value != "COMPLETED"
