"""
AI Research Backtesting API route tests -- feature-disabled behavior,
execution-field rejection, cost-guard limits, the full lifecycle observed
through real endpoints, drill-down to the underlying research_id, and the
routing-order guarantee (Section 22: /backtests must never be swallowed by
the single-research router's /{research_id} catch-all). run_research()/
settle_pending_decisions() are monkeypatched -- no real LLM/network call.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

from trading.ai_research.backtest import service
from trading.ai_research.trading_agents_adapter import ResearchReport, ResearchResult

_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
_WEEK_AGO = (date.today() - timedelta(days=7)).isoformat()


@pytest.fixture(autouse=True)
def _fresh_semaphore():
    service._reset_semaphore()
    yield
    service._reset_semaphore()


def _fake_result(decision="Rating: Hold", signal="Hold"):
    return ResearchResult(
        instrument="AAPL", as_of_date=_YESTERDAY,
        report=ResearchReport(final_trade_decision=decision, signal=signal),
        raw_state={},
    )


def test_requires_auth(client):
    assert client.post("/api/ai-research/backtests", json={
        "symbols": ["AAPL"], "start_date": _WEEK_AGO, "end_date": _YESTERDAY,
    }).status_code == 401


def test_disabled_by_default_returns_200_not_error(client, viewer_auth, monkeypatch):
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    r = client.post(
        "/api/ai-research/backtests", headers=viewer_auth,
        json={"symbols": ["AAPL"], "start_date": _WEEK_AGO, "end_date": _YESTERDAY},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_backtests_route_is_not_swallowed_by_research_id_catchall(client, viewer_auth, monkeypatch):
    """Section 22 routing note: GET /api/ai-research/backtests must hit the
    backtest history endpoint, never the single-research
    GET /{research_id} catch-all with research_id='backtests'."""
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.get("/api/ai-research/backtests", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body, f"unexpected shape (got the single-research 404 path?): {body}"


@pytest.mark.parametrize("field,value", [
    ("broker", "angelone"), ("account_id", "ACC1"), ("quantity", 100),
    ("side", "BUY"), ("strategy_id", "CombinedVwapNifty"), ("live", True),
])
def test_execution_fields_rejected_with_422(client, viewer_auth, monkeypatch, field, value):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    body = {"symbols": ["AAPL"], "start_date": _WEEK_AGO, "end_date": _YESTERDAY, field: value}
    r = client.post("/api/ai-research/backtests", headers=viewer_auth, json=body)
    assert r.status_code == 422, f"{field} was not rejected: {r.status_code} {r.text}"


def test_cost_guard_rejects_too_many_symbols(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("AI_BACKTEST_MAX_SYMBOLS", "1")
    r = client.post(
        "/api/ai-research/backtests", headers=viewer_auth,
        json={"symbols": ["AAPL", "MSFT"], "start_date": _WEEK_AGO, "end_date": _YESTERDAY},
    )
    assert r.status_code == 422
    assert "symbols" in r.json()["detail"].lower()


def test_estimate_never_creates_a_job(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.post(
        "/api/ai-research/backtests/estimate", headers=viewer_auth,
        json={"symbols": ["AAPL"], "start_date": _WEEK_AGO, "end_date": _YESTERDAY, "frequency": "daily"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_runs"] == body["symbol_count"] * body["date_count"]
    history = client.get("/api/ai-research/backtests", headers=viewer_auth).json()
    assert history["total"] == 0


def test_data_quality_endpoint_reachable_even_when_disabled(client, viewer_auth, monkeypatch):
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    r = client.get("/api/ai-research/backtests/data-quality", headers=viewer_auth)
    assert r.status_code == 200
    sources = {s["source"]: s["safe"] for s in r.json()["sources"]}
    assert sources["market_data"] == "YES"
    assert sources["fundamentals_financial_statements"] == "PARTIAL"


def test_full_lifecycle_via_api(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", lambda *a, **k: _fake_result())
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    submit = client.post(
        "/api/ai-research/backtests", headers=viewer_auth,
        json={"symbols": ["AAPL"], "start_date": _YESTERDAY, "end_date": _YESTERDAY},
    )
    assert submit.status_code == 200
    body = submit.json()
    assert body["status"] == "QUEUED"
    assert body["total_runs"] == 1
    backtest_id = body["backtest_id"]

    status = None
    for _ in range(100):
        status = client.get(f"/api/ai-research/backtests/{backtest_id}/status", headers=viewer_auth).json()
        if status["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.02)
    assert status["status"] == "COMPLETED"
    assert status["completed_runs"] == 1
    assert status["progress"] == 1.0

    detail = client.get(f"/api/ai-research/backtests/{backtest_id}", headers=viewer_auth).json()
    assert detail["metrics"]["label"] == "AI Research Decision Evaluation"

    runs = client.get(f"/api/ai-research/backtests/{backtest_id}/runs", headers=viewer_auth).json()
    assert runs["total"] == 1
    cell = runs["items"][0]
    assert cell["status"] == "COMPLETED"
    assert cell["research_id"] is not None

    # Drill-down (Section 27): the cell's research_id resolves to the full,
    # first-class single-research result -- no duplicated report storage.
    drilldown = client.get(f"/api/ai-research/{cell['research_id']}", headers=viewer_auth)
    assert drilldown.status_code == 200
    assert drilldown.json()["status"] == "COMPLETED"

    metrics = client.get(f"/api/ai-research/backtests/{backtest_id}/metrics", headers=viewer_auth).json()
    assert metrics["label"] == "AI Research Decision Evaluation"

    history = client.get("/api/ai-research/backtests", headers=viewer_auth).json()
    assert any(h["backtest_id"] == backtest_id for h in history["items"])


def test_unknown_backtest_id_404s(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    assert client.get("/api/ai-research/backtests/does-not-exist", headers=viewer_auth).status_code == 404
    assert client.get("/api/ai-research/backtests/does-not-exist/status", headers=viewer_auth).status_code == 404
    assert client.get("/api/ai-research/backtests/does-not-exist/runs", headers=viewer_auth).status_code == 404


def test_cancel_and_resume_via_api(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")

    def _slow(*a, **k):
        time.sleep(0.05)
        return _fake_result()

    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", _slow)
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    submit = client.post(
        "/api/ai-research/backtests", headers=viewer_auth,
        json={"symbols": ["AAPL"], "start_date": _WEEK_AGO, "end_date": _YESTERDAY, "frequency": "daily"},
    )
    backtest_id = submit.json()["backtest_id"]

    cancel = client.post(f"/api/ai-research/backtests/{backtest_id}/cancel", headers=viewer_auth)
    assert cancel.status_code == 200

    status = None
    for _ in range(100):
        status = client.get(f"/api/ai-research/backtests/{backtest_id}/status", headers=viewer_auth).json()
        if status["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.02)
    assert status["status"] == "CANCELLED"

    # A CANCELLED backtest may be resumed -- must be async (create_task).
    resume = client.post(f"/api/ai-research/backtests/{backtest_id}/resume", headers=viewer_auth)
    assert resume.status_code == 200
    assert resume.json()["resumed_pending_cells"] >= 1
