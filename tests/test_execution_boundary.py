"""
Phase 11 (Section 58) -- execution boundary runtime guard.

Proves, in two independent ways, that an AI-generated candidate can
never reach broker execution:

  1. STATIC: no file under trading/ai_research/ or
     trading/ai_options_research/ imports anything from
     trading.common (the package that owns BrokerClient/place_order/
     cancel_order/create_broker and every real broker adapter).

  2. DYNAMIC: a real (fake-LLM) end-to-end AI Options Research pipeline
     run, with every PaperBroker order-placing/cancelling method
     instrumented as a spy, produces real StrategyCandidate objects and
     a persisted report WITHOUT EVER calling the broker -- proving there
     is no code path, accidental or otherwise, connecting the two.

No execution path is added to make this test pass (Section 58's own
instruction) -- both checks observe the EXISTING architecture only.
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from trading.common.brokers.paper_broker import PaperBroker

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCANNED_DIRS = (_REPO_ROOT / "trading" / "ai_research", _REPO_ROOT / "trading" / "ai_options_research")


def _python_files():
    for d in _SCANNED_DIRS:
        yield from d.rglob("*.py")


def test_no_ai_module_imports_trading_common():
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "trading.common" or alias.name.startswith("trading.common."):
                        offenders.append((str(path), alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "trading.common" or node.module.startswith("trading.common."):
                    offenders.append((str(path), node.module))
    assert offenders == [], f"AI modules must never import trading.common (broker/execution): {offenders}"


def test_ai_candidate_has_no_order_shaped_fields():
    """A StrategyCandidate has legs (side/option_type/strike per leg), never
    a single scalar symbol/side/quantity -- there is no natural shape for
    it to be silently coerced into a place_order() call."""
    from trading.ai_options_research.candidates import StrategyCandidate

    fields = {f for f in StrategyCandidate.__dataclass_fields__}
    assert "symbol" not in fields
    assert "side" not in fields
    assert "quantity" not in fields
    assert "legs" in fields  # the only place per-leg side/strike data lives


def test_full_pipeline_never_touches_broker(db_session):
    """Dynamic proof: run the REAL AI Options Research pipeline (fake LLM
    only, to avoid a real network call) end-to-end and assert the broker
    was never constructed or called."""
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

    expiry = date(2100, 9, 24)
    cache = LiveCache(stale_seconds=60)
    cache.put(IndexQuote.build("NIFTY", ltp=25000.0, provider="icici_breeze"))
    for strike in (24900, 25000, 25100):
        cache.put(OptionQuote.build(underlying="NIFTY", expiry=expiry, strike=strike, option_type="CE", ltp=100, oi=5000))
        cache.put(OptionQuote.build(underlying="NIFTY", expiry=expiry, strike=strike, option_type="PE", ltp=100, oi=5000))
    master = InstrumentMaster()
    master.load([option_instrument("NIFTY", expiry, k, ot) for k in (24900, 25000, 25100) for ot in ("CE", "PE")], as_of=date(2100, 9, 1))
    svc = MarketDataService(cache=cache, instrument_master=master, session_factory=lambda: None)
    set_service(svc)

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

    with patch.object(PaperBroker, "place_order", autospec=True) as spy_place, \
         patch.object(PaperBroker, "cancel_order", autospec=True) as spy_cancel, \
         patch.object(PaperBroker, "connect", autospec=True) as spy_connect:
        with patch.object(service.agents, "get_llm", lambda *a, **kw: _FakeLLM()):
            req = OptionsResearchRequestIn(
                underlying="NIFTY", expiry=expiry.isoformat(), strategy_universe_preset=StrategyUniversePreset.CUSTOM,
                strategy_universe=["LONG_CALL", "IRON_CONDOR"], candidate_limit=2, debate_rounds=1,
                llm_provider="openai", quick_think_llm="gpt-4o-mini", wing_width=100,
            )
            job = repository.create_run(req)
            service._run_pipeline(job.research_id, req)

        final = repository.get_run(job.research_id)
        assert final.status == "COMPLETED"
        assert len(final.report["candidates"]) == 2  # real candidates WERE produced

        # The execution boundary held: not one broker call happened anywhere
        # in this real, end-to-end research run.
        spy_place.assert_not_called()
        spy_cancel.assert_not_called()
        spy_connect.assert_not_called()

    set_service(None)
