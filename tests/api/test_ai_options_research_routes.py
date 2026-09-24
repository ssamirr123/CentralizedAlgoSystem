"""Phase 10 -- AI Options Research API tests (Sections 41, 58/59 API surface, 67/68)."""
from __future__ import annotations

from datetime import date

import pytest

from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.schemas import IndexQuote, OptionQuote
from trading.market_data.service import MarketDataService, set_service
from trading.market_data.symbols import option_instrument

EXPIRY = date(2100, 9, 24)


@pytest.fixture
def market(db_session):
    cache = LiveCache(stale_seconds=60)
    cache.put(IndexQuote.build("NIFTY", ltp=25000.0, provider="icici_breeze"))
    for strike in (24900, 25000, 25100):
        cache.put(OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=strike, option_type="CE", ltp=100, oi=5000))
        cache.put(OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=strike, option_type="PE", ltp=100, oi=5000))
    master = InstrumentMaster()
    master.load([option_instrument("NIFTY", EXPIRY, k, ot) for k in (24900, 25000, 25100) for ot in ("CE", "PE")], as_of=date(2100, 9, 1))
    svc = MarketDataService(cache=cache, instrument_master=master, session_factory=lambda: None)
    set_service(svc)
    yield svc
    set_service(None)


def test_preview_requires_auth(client, market):
    assert client.get("/api/ai-options-research/preview?underlying=NIFTY").status_code == 401


def test_preview_disabled_by_default_returns_200(client, viewer_auth, market, monkeypatch):
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    r = client.get("/api/ai-options-research/preview?underlying=NIFTY", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json()["disabled"] is True


def test_preview_returns_snapshot_facts(client, viewer_auth, market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.get(f"/api/ai-options-research/preview?underlying=NIFTY&expiry={EXPIRY.isoformat()}", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["underlying"] == "NIFTY"
    assert body["atm_strike"] == 25000
    assert "evidence_quality" in body


def test_submit_disabled_returns_200_not_error(client, viewer_auth, market, monkeypatch):
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    r = client.post("/api/ai-options-research", json={"underlying": "NIFTY"}, headers=viewer_auth)
    assert r.status_code == 200
    assert r.json()["disabled"] is True


def test_submit_rejects_unsupported_underlying(client, viewer_auth, market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.post("/api/ai-options-research", json={"underlying": "DOWJONES"}, headers=viewer_auth)
    assert r.status_code == 422


def test_submit_rejects_excess_candidate_limit(client, viewer_auth, market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("AI_OPTIONS_MAX_CANDIDATES", "2")
    r = client.post("/api/ai-options-research", json={"underlying": "NIFTY", "candidate_limit": 5}, headers=viewer_auth)
    assert r.status_code == 422


def test_unknown_research_id_404(client, viewer_auth, market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    assert client.get("/api/ai-options-research/does-not-exist", headers=viewer_auth).status_code == 404
    assert client.get("/api/ai-options-research/does-not-exist/status", headers=viewer_auth).status_code == 404
    assert client.get("/api/ai-options-research/does-not-exist/stages", headers=viewer_auth).status_code == 404


def test_history_returns_empty_list_initially(client, viewer_auth, market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.get("/api/ai-options-research/history", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_no_order_or_strategy_endpoints_exist(client, viewer_auth, market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    for path in ("/api/ai-options-research/order", "/api/ai-options-research/execute", "/api/ai-options-research/strategy"):
        assert client.get(path, headers=viewer_auth).status_code == 404


def test_submit_and_track_full_lifecycle_with_fake_llm(client, viewer_auth, market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    from trading.ai_options_research import service as options_service
    from trading.ai_options_research.agent_schemas import (
        Confidence, DirectionalCaseOutput, FinalReportOutput, MarketRegimeOutput,
        OIPositioningOutput, RiskAnalysisOutput, StrategyResearchOutput, VolatilityAnalysisOutput,
    )

    class _FakeStructured:
        def __init__(self, value):
            self._value = value

        def invoke(self, prompt):
            return self._value

    class _FakeLLM:
        def with_structured_output(self, schema):
            defaults = {
                MarketRegimeOutput: MarketRegimeOutput(trend="SIDEWAYS", volatility_regime="NORMAL", market_structure="RANGE_BOUND", evidence="e", confidence=Confidence.HIGH),
                VolatilityAnalysisOutput: VolatilityAnalysisOutput(interpretation="calm", confidence=Confidence.HIGH),
                OIPositioningOutput: OIPositioningOutput(interpretation="balanced", confidence=Confidence.HIGH),
                StrategyResearchOutput: StrategyResearchOutput(commentary=[]),
                DirectionalCaseOutput: DirectionalCaseOutput(thesis="t", key_levels=[25000.0]),
                RiskAnalysisOutput: RiskAnalysisOutput(analyses=[]),
                FinalReportOutput: FinalReportOutput(market_overview="o", technical_context="n", options_market_structure_summary="s", final_summary="AI OPTIONS RESEARCH done"),
            }
            return _FakeStructured(defaults[schema])

    monkeypatch.setattr(options_service.agents, "get_llm", lambda *a, **kw: _FakeLLM())

    r = client.post(
        "/api/ai-options-research",
        json={"underlying": "NIFTY", "expiry": EXPIRY.isoformat(), "strategy_universe_preset": "CUSTOM", "strategy_universe": ["LONG_CALL"], "candidate_limit": 1},
        headers=viewer_auth,
    )
    assert r.status_code == 200
    research_id = r.json()["research_id"]

    import time
    for _ in range(50):
        status = client.get(f"/api/ai-options-research/{research_id}/status", headers=viewer_auth).json()
        if status["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.1)
    assert status["status"] == "COMPLETED"

    result = client.get(f"/api/ai-options-research/{research_id}", headers=viewer_auth).json()
    assert result["report"]["final_summary"] == "AI OPTIONS RESEARCH done"
    assert len(result["report"]["candidates"]) == 1

    history = client.get("/api/ai-options-research/history", headers=viewer_auth).json()
    assert history["total"] == 1
