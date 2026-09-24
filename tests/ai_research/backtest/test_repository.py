"""
Backtest persistence tests: cell creation, lifecycle updates, restart
durability, interrupted-job recovery, and cancellation flag.
"""
from __future__ import annotations

from trading.ai_research.backtest import repository
from trading.ai_research.backtest.schemas import BacktestRequestIn


def _request(**overrides) -> BacktestRequestIn:
    body = {"symbols": ["AAPL"], "start_date": "2026-01-01", "end_date": "2026-01-31"}
    body.update(overrides)
    return BacktestRequestIn.model_validate(body)


def test_create_backtest_creates_job_and_cells():
    dates = ("2026-01-01", "2026-01-08", "2026-01-15")
    backtest_id = repository.create_backtest(_request(), dates, holding_days=5)
    job = repository.get_backtest(backtest_id)
    assert job is not None
    assert job.status == "QUEUED"
    assert job.total_runs == 3
    cells = repository.get_cells(backtest_id)
    assert len(cells) == 3
    assert all(c.status == "PENDING" for c in cells)


def test_create_backtest_multi_symbol_grid():
    dates = ("2026-01-01", "2026-01-08")
    backtest_id = repository.create_backtest(_request(symbols=["AAPL", "MSFT"]), dates, holding_days=5)
    job = repository.get_backtest(backtest_id)
    assert job.total_runs == 4
    assert len(repository.get_cells(backtest_id)) == 4


def test_cell_lifecycle_running_completed():
    dates = ("2026-01-01",)
    backtest_id = repository.create_backtest(_request(), dates, holding_days=5)
    repository.mark_backtest_running(backtest_id)
    repository.mark_cell_running(backtest_id, "AAPL", "2026-01-01")

    job = repository.get_backtest(backtest_id)
    assert job.status == "RUNNING"
    assert job.current_symbol == "AAPL"

    repository.mark_cell_completed(
        backtest_id, "AAPL", "2026-01-01", research_id="r-1",
        raw_decision="Rating: Buy", normalized_decision="Buy",
    )
    job = repository.get_backtest(backtest_id)
    assert job.completed_runs == 1
    cell = repository.get_cells(backtest_id)[0]
    assert cell.status == "COMPLETED"
    assert cell.research_id == "r-1"
    assert cell.normalized_decision == "Buy"
    assert cell.evaluation_status == "PENDING"


def test_cell_failure_increments_failed_runs_and_persists_error():
    backtest_id = repository.create_backtest(_request(), ("2026-01-01",), holding_days=5)
    repository.mark_cell_failed(backtest_id, "AAPL", "2026-01-01", "simulated provider failure")
    job = repository.get_backtest(backtest_id)
    assert job.failed_runs == 1
    cell = repository.get_cells(backtest_id)[0]
    assert cell.status == "FAILED"
    assert "simulated" in cell.error_message


def test_cell_evaluation_persists_forward_return():
    backtest_id = repository.create_backtest(_request(), ("2026-01-01",), holding_days=5)
    repository.mark_cell_completed(
        backtest_id, "AAPL", "2026-01-01", research_id="r-1",
        raw_decision="Buy", normalized_decision="Buy",
    )
    repository.mark_cell_evaluated(
        backtest_id, "AAPL", "2026-01-01", raw_return=0.021, alpha_return=0.015,
        benchmark="SPY", holding_days=5, resolution_date="2026-01-08",
    )
    cell = repository.get_cells(backtest_id)[0]
    assert cell.evaluation_status == "RESOLVED"
    assert cell.alpha_return == 0.015
    assert cell.benchmark == "SPY"


def test_backtest_completed_stores_metrics_survives_a_new_session():
    """Restart durability (Section 19): a fresh repository call (no shared
    in-memory object) sees the same persisted result."""
    backtest_id = repository.create_backtest(_request(), ("2026-01-01",), holding_days=5)
    repository.mark_cell_completed(backtest_id, "AAPL", "2026-01-01", research_id="r-1", raw_decision="Hold", normalized_decision="Hold")
    metrics = {"label": "AI Research Decision Evaluation", "total_decisions": 1}
    repository.mark_backtest_completed(backtest_id, metrics)

    reloaded = repository.get_backtest(backtest_id)
    assert reloaded.status == "COMPLETED"
    assert reloaded.metrics["total_decisions"] == 1


def test_recover_interrupted_marks_stale_running_as_failed():
    backtest_id = repository.create_backtest(_request(), ("2026-01-01", "2026-01-08"), holding_days=5)
    repository.mark_backtest_running(backtest_id)
    repository.mark_cell_running(backtest_id, "AAPL", "2026-01-01")  # simulate a process that died mid-run

    recovered = repository.recover_interrupted_backtests()
    assert recovered == 1

    job = repository.get_backtest(backtest_id)
    assert job.status == "FAILED"
    running_cell = next(c for c in repository.get_cells(backtest_id) if c.cell_date.isoformat() == "2026-01-01")
    assert running_cell.status == "FAILED"
    # The cell that was never even started is untouched -- resume can still pick it up.
    pending_cell = next(c for c in repository.get_cells(backtest_id) if c.cell_date.isoformat() == "2026-01-08")
    assert pending_cell.status == "PENDING"


def test_recover_interrupted_never_touches_completed_cells():
    backtest_id = repository.create_backtest(_request(), ("2026-01-01", "2026-01-08"), holding_days=5)
    repository.mark_backtest_running(backtest_id)
    repository.mark_cell_completed(backtest_id, "AAPL", "2026-01-01", research_id="r-1", raw_decision="Hold", normalized_decision="Hold")
    repository.mark_cell_running(backtest_id, "AAPL", "2026-01-08")

    repository.recover_interrupted_backtests()
    cells = {c.cell_date.isoformat(): c.status for c in repository.get_cells(backtest_id)}
    assert cells["2026-01-01"] == "COMPLETED", "an already-COMPLETED cell must never be reverted"
    assert cells["2026-01-08"] == "FAILED"


def test_recover_interrupted_ignores_terminal_backtests():
    backtest_id = repository.create_backtest(_request(), ("2026-01-01",), holding_days=5)
    repository.mark_cell_completed(backtest_id, "AAPL", "2026-01-01", research_id="r-1", raw_decision="Hold", normalized_decision="Hold")
    repository.mark_backtest_completed(backtest_id, {"label": "AI Research Decision Evaluation"})

    recovered = repository.recover_interrupted_backtests()
    assert recovered == 0
    assert repository.get_backtest(backtest_id).status == "COMPLETED"


def test_get_not_completed_cells_excludes_completed_only():
    backtest_id = repository.create_backtest(_request(), ("2026-01-01", "2026-01-08", "2026-01-15"), holding_days=5)
    repository.mark_cell_completed(backtest_id, "AAPL", "2026-01-01", research_id="r-1", raw_decision="Hold", normalized_decision="Hold")
    repository.mark_cell_failed(backtest_id, "AAPL", "2026-01-08", "boom")

    not_completed = repository.get_not_completed_cells(backtest_id)
    dates = {d for _, d in not_completed}
    assert dates == {"2026-01-08", "2026-01-15"}


def test_cancel_flag_lifecycle():
    backtest_id = repository.create_backtest(_request(), ("2026-01-01",), holding_days=5)
    assert repository.request_cancel(backtest_id) is True
    assert repository.is_cancel_requested(backtest_id) is True
    repository.clear_cancel_flag(backtest_id)
    assert repository.is_cancel_requested(backtest_id) is False


def test_cancel_rejected_for_terminal_backtest():
    backtest_id = repository.create_backtest(_request(), ("2026-01-01",), holding_days=5)
    repository.mark_backtest_completed(backtest_id, {"label": "AI Research Decision Evaluation"})
    assert repository.request_cancel(backtest_id) is False


def test_list_backtests_pagination_and_filter():
    for i in range(3):
        repository.create_backtest(_request(symbols=[f"SYM{i}"]), ("2026-01-01",), holding_days=5)
    page1, total = repository.list_backtests(page=1, page_size=2)
    assert len(page1) == 2
    assert total == 3


def test_list_cells_page_filters_by_symbol():
    backtest_id = repository.create_backtest(_request(symbols=["AAPL", "MSFT"]), ("2026-01-01",), holding_days=5)
    rows, total = repository.list_cells_page(backtest_id, symbol="AAPL")
    assert total == 1
    assert rows[0].symbol == "AAPL"


def test_no_secret_fields_exist_on_backtest_models():
    from trading.ai_research.backtest.models import AIBacktestJob, AIBacktestRun

    forbidden = ("api_key", "apikey", "secret", "token", "password", "credential")
    for model in (AIBacktestJob, AIBacktestRun):
        columns = {c.name.lower() for c in model.__table__.columns}
        offending = [c for c in columns if any(bad in c for bad in forbidden)]
        assert not offending, f"{model.__name__} has a secret-shaped column: {offending}"
