"""Phase 11 (Section 51/52) -- controlled, mocked load test.

No real LLM/Breeze call is made (fake LLM, in-memory market data cache)
-- this measures the app's own concurrency handling (semaphore, DB
writes under concurrent access, no cross-request data leakage), not
external provider latency."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from trading.ai_options_research import repository, service
from trading.ai_options_research.agent_schemas import (
    Confidence, DirectionalCaseOutput, FinalReportOutput, MarketRegimeOutput,
    OIPositioningOutput, RiskAnalysisOutput, StrategyResearchOutput, VolatilityAnalysisOutput,
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


def test_concurrent_research_submissions_no_cross_contamination(market, monkeypatch):
    """Section 51/52: N concurrent research submissions each complete
    with their OWN correct data -- no shared-state leakage between
    concurrently-running jobs, and no unhandled exception."""
    monkeypatch.setattr(service.agents, "get_llm", lambda *a, **kw: _FakeLLM())

    def _submit_and_run(candidate_limit: int):
        req = OptionsResearchRequestIn(
            underlying="NIFTY", expiry=EXPIRY.isoformat(), strategy_universe_preset=StrategyUniversePreset.CUSTOM,
            strategy_universe=["LONG_CALL", "LONG_PUT"][:candidate_limit], candidate_limit=candidate_limit, debate_rounds=1,
            llm_provider="openai", quick_think_llm="gpt-4o-mini",
        )
        job = repository.create_run(req)
        service._run_pipeline(job.research_id, req)
        return job.research_id, candidate_limit

    n = 5
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(_submit_and_run, [1, 2, 1, 2, 1]))
    duration = time.monotonic() - start

    failures = []
    for research_id, expected_candidates in results:
        final = repository.get_run(research_id)
        if final.status != "COMPLETED":
            failures.append((research_id, final.status, final.error))
            continue
        if len(final.report["candidates"]) != expected_candidates:
            failures.append((research_id, "wrong candidate count", len(final.report["candidates"])))

    assert failures == [], f"concurrent submissions produced errors/cross-contamination: {failures}"
    assert duration < 30, f"5 mocked concurrent jobs took {duration:.1f}s -- unexpectedly slow"


def test_concurrent_history_reads_are_consistent(market, monkeypatch):
    """History reads under concurrent load never raise and always return
    a bounded page (Section 39/51)."""
    monkeypatch.setattr(service.agents, "get_llm", lambda *a, **kw: _FakeLLM())
    req = OptionsResearchRequestIn(
        underlying="NIFTY", expiry=EXPIRY.isoformat(), strategy_universe_preset=StrategyUniversePreset.CUSTOM,
        strategy_universe=["LONG_CALL"], candidate_limit=1, debate_rounds=1,
        llm_provider="openai", quick_think_llm="gpt-4o-mini",
    )
    for _ in range(3):
        job = repository.create_run(req)
        service._run_pipeline(job.research_id, req)

    def _read_history(_):
        jobs, total = repository.list_runs(1, 10)
        assert total >= 3
        assert len(jobs) <= 10  # bounded, never an unbounded scan
        return True

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(_read_history, range(10)))
    assert all(results)
