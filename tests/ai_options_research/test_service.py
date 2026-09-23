"""Phase 10 -- job lifecycle, data-quality gate, and restart-durability
tests. LLM calls are replaced with a fake structured-output client so
these tests never make a real network call."""
from __future__ import annotations

from datetime import date

import pytest

from trading.ai_options_research import repository, service
from trading.ai_options_research.agent_schemas import (
    Confidence,
    DirectionalCaseOutput,
    FinalReportOutput,
    MarketRegimeOutput,
    OIPositioningOutput,
    RiskAnalysisOutput,
    StrategyResearchOutput,
    VolatilityAnalysisOutput,
)
from trading.ai_options_research.schemas import OptionsResearchRequestIn, StrategyUniversePreset
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.schemas import IndexQuote, OptionQuote
from trading.market_data.service import MarketDataService, set_service
from trading.market_data.symbols import option_instrument

EXPIRY = date(2100, 9, 24)


class _FakeStructured:
    def __init__(self, value):
        self._value = value

    def invoke(self, prompt):
        return self._value


class _FakeLLM:
    """Returns a fixed value for whichever Pydantic schema is requested,
    regardless of prompt content -- deterministic for testing."""

    def with_structured_output(self, schema):
        defaults = {
            MarketRegimeOutput: MarketRegimeOutput(
                trend="SIDEWAYS", volatility_regime="NORMAL", market_structure="RANGE_BOUND",
                evidence="test evidence", confidence=Confidence.HIGH,
            ),
            VolatilityAnalysisOutput: VolatilityAnalysisOutput(interpretation="calm", confidence=Confidence.HIGH),
            OIPositioningOutput: OIPositioningOutput(interpretation="balanced", confidence=Confidence.HIGH),
            StrategyResearchOutput: StrategyResearchOutput(commentary=[]),
            DirectionalCaseOutput: DirectionalCaseOutput(thesis="fake thesis", key_levels=[25000.0]),
            RiskAnalysisOutput: RiskAnalysisOutput(analyses=[]),
            FinalReportOutput: FinalReportOutput(
                market_overview="overview", technical_context="none", options_market_structure_summary="summary",
                final_summary="AI OPTIONS RESEARCH -- final summary",
            ),
        }
        return _FakeStructured(defaults[schema])


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


def _request(**overrides):
    defaults = dict(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strategy_universe_preset=StrategyUniversePreset.CUSTOM,
        strategy_universe=["LONG_CALL", "IRON_CONDOR"], candidate_limit=2, debate_rounds=1,
        llm_provider="openai", quick_think_llm="gpt-4o-mini", wing_width=100,
    )
    defaults.update(overrides)
    return OptionsResearchRequestIn(**defaults)


def test_full_pipeline_completes_with_fake_llm(market, monkeypatch):
    monkeypatch.setattr(service.agents, "get_llm", lambda *a, **kw: _FakeLLM())
    job = repository.create_run(_request())
    service._run_pipeline(job.research_id, _request())
    final = repository.get_run(job.research_id)
    assert final.status == "COMPLETED"
    assert final.research_snapshot_id is not None
    assert final.evidence_quality in ("OK", "DEGRADED")
    assert len(final.report["candidates"]) == 2
    assert "AI OPTIONS RESEARCH" in final.report["final_summary"]
    stage_names = [s.name for s in final.node_stages]
    assert [s.status for s in final.node_stages] == ["COMPLETED"] * len(stage_names)


def test_data_quality_gate_short_circuits_without_llm_calls(market, monkeypatch):
    calls = []
    monkeypatch.setattr(service.agents, "get_llm", lambda *a, **kw: calls.append(1) or _FakeLLM())
    # Force MISSING quality: an expiry with genuinely no chain data at all.
    empty_expiry = date(2100, 10, 1)
    market.master.load([], as_of=date(2100, 9, 1))  # wipe strikes -- resolve_expiry will find none
    req = _request(expiry="2100-10-01")
    job = repository.create_run(req)
    service._run_pipeline(job.research_id, req)
    final = repository.get_run(job.research_id)
    assert final.status in ("COMPLETED", "FAILED")
    assert calls == []  # never reached the LLM


def test_bounded_debate_rounds_never_loops_unboundedly(market, monkeypatch):
    monkeypatch.setattr(service.agents, "get_llm", lambda *a, **kw: _FakeLLM())
    req = _request(debate_rounds=2)
    job = repository.create_run(req)
    service._run_pipeline(job.research_id, req)
    final = repository.get_run(job.research_id)
    assert final.status == "COMPLETED"


def test_restart_durability_fresh_session_sees_completed_run(market, monkeypatch):
    monkeypatch.setattr(service.agents, "get_llm", lambda *a, **kw: _FakeLLM())
    job = repository.create_run(_request())
    service._run_pipeline(job.research_id, _request())

    # Simulate a restart: a brand-new repository call (new SessionLocal()
    # internally) must still see the persisted result.
    reloaded = repository.get_run(job.research_id)
    assert reloaded.status == "COMPLETED"
    assert reloaded.report is not None


def test_recover_interrupted_jobs_marks_running_as_failed(market):
    job = repository.create_run(_request())
    repository.mark_running(job.research_id)
    assert repository.get_run(job.research_id).status == "RUNNING"

    recovered = repository.recover_interrupted_jobs()
    assert recovered >= 1
    final = repository.get_run(job.research_id)
    assert final.status == "FAILED"
    assert "restart" in final.error.lower() or "interrupted" in final.error.lower()


def test_cost_guard_rejects_excess_candidate_limit(market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("AI_OPTIONS_MAX_CANDIDATES", "2")
    req = _request(candidate_limit=5)
    with pytest.raises(service.OptionsResearchConfigError):
        service.submit_research(req)


def test_cost_guard_rejects_excess_debate_rounds(market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("AI_OPTIONS_MAX_DEBATE_ROUNDS", "1")
    req = _request(debate_rounds=3)
    with pytest.raises(service.OptionsResearchConfigError):
        service.submit_research(req)


def test_disabled_by_default(market, monkeypatch):
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    with pytest.raises(service.OptionsResearchDisabledError):
        service.submit_research(_request())


def test_unsupported_underlying_rejected(market, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    req = _request(underlying="DOWJONES")
    with pytest.raises(service.OptionsResearchConfigError):
        service.submit_research(req)
