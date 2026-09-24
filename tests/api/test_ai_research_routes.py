"""
AI Research API route tests -- feature-disabled behavior (Section 4),
execution-field rejection at the HTTP layer (Section 6/18), and the
QUEUED -> COMPLETED lifecycle observed through the real endpoints
(Section 21/22). run_research() is monkeypatched so no real LLM/network
call is ever made from a test.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

from trading.ai_research import service
from trading.ai_research.trading_agents_adapter import ResearchReport, ResearchResult

_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


@pytest.fixture(autouse=True)
def _fresh_semaphore():
    service._reset_semaphore()
    yield
    service._reset_semaphore()


def test_requires_auth(client):
    assert client.post("/api/ai-research", json={"symbol": "AAPL", "research_date": _YESTERDAY}).status_code == 401


def test_disabled_by_default_returns_200_not_error(client, viewer_auth, monkeypatch):
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    r = client.post(
        "/api/ai-research", headers=viewer_auth,
        json={"symbol": "AAPL", "research_date": _YESTERDAY},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_disabled_history_and_status_also_return_disabled(client, viewer_auth, monkeypatch):
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    assert client.get("/api/ai-research/history", headers=viewer_auth).json()["enabled"] is False
    assert client.get("/api/ai-research/some-id", headers=viewer_auth).json()["enabled"] is False
    assert client.get("/api/ai-research/some-id/status", headers=viewer_auth).json()["enabled"] is False


def test_config_options_always_reachable_even_when_disabled(client, viewer_auth, monkeypatch):
    """Section 20: the feature being off must never make its own
    discovery endpoint (or, per that section, the rest of the backend)
    unhealthy."""
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    r = client.get("/api/ai-research/config/options", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert "market" in body["analysts"]
    assert "openai" in body["llm_providers"]


@pytest.mark.parametrize("field,value", [
    ("broker", "angelone"), ("account_id", "ACC1"), ("quantity", 100),
    ("side", "BUY"), ("strategy_id", "CombinedVwapNifty"), ("live", True),
])
def test_execution_fields_rejected_with_422(client, viewer_auth, monkeypatch, field, value):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    body = {"symbol": "AAPL", "research_date": _YESTERDAY, field: value}
    r = client.post("/api/ai-research", headers=viewer_auth, json=body)
    assert r.status_code == 422, f"{field} was not rejected: {r.status_code} {r.text}"


def test_health_endpoint_unaffected_by_ai_research_state(client, monkeypatch):
    """Section 20: /api/health must keep working regardless of
    AI_RESEARCH_ENABLED."""
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    assert client.get("/api/health").status_code == 200
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    assert client.get("/api/health").status_code == 200


def test_full_lifecycle_via_api(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    fake_result = ResearchResult(
        instrument="AAPL", as_of_date=_YESTERDAY,
        report=ResearchReport(
            market_analysis="bullish setup", bull_case="strong earnings",
            bear_case="valuation stretched", final_trade_decision="Hold", signal="Hold",
        ),
        raw_state={},
    )
    monkeypatch.setattr("trading.ai_research.service.run_research", lambda *a, **k: fake_result)

    submit = client.post(
        "/api/ai-research", headers=viewer_auth,
        json={"symbol": "AAPL", "research_date": _YESTERDAY, "research_depth": "quick"},
    )
    assert submit.status_code == 200
    body = submit.json()
    assert body["status"] == "QUEUED"
    research_id = body["research_id"]

    for _ in range(50):
        status = client.get(f"/api/ai-research/{research_id}/status", headers=viewer_auth).json()
        if status["status"] == "COMPLETED":
            break
        time.sleep(0.02)
    assert status["status"] == "COMPLETED"

    result = client.get(f"/api/ai-research/{research_id}", headers=viewer_auth).json()
    assert result["report"]["bull_case"] == "strong earnings"
    assert result["report"]["bear_case"] == "valuation stretched"
    assert result["report"]["signal"] == "Hold"

    stages = client.get(f"/api/ai-research/{research_id}/stages", headers=viewer_auth).json()
    assert stages["status"] == "COMPLETED"
    assert all(s["status"] == "COMPLETED" for s in stages["stages"])
    assert {"Market Analyst", "Trader", "Portfolio Manager"} <= {s["name"] for s in stages["stages"]}

    history = client.get("/api/ai-research/history", headers=viewer_auth).json()
    assert any(h["research_id"] == research_id for h in history["items"])
    assert history["total"] >= 1


def test_unknown_research_id_404s(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.get("/api/ai-research/does-not-exist", headers=viewer_auth)
    assert r.status_code == 404
