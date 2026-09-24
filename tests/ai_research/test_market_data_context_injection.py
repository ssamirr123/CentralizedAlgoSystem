"""
Phase 8 Section 23: proves market_data_context is appended to the real
upstream `instrument_context` AgentState field (verified against
propagation.py's Propagator.create_initial_state -- see
trading_agents_adapter.py's own docstring citation), via a minimal
duck-typed fake graph -- no real tradingagents/LLM call.
"""
from __future__ import annotations

from contextlib import contextmanager

from trading.ai_research.trading_agents_adapter import _run_graph_with_progress


class _FakeGraphObj:
    """Duck-types just the real, public TradingAgentsGraph methods
    _run_graph_with_progress calls (see that function's own docstring
    for why each one is the genuine upstream method)."""

    def __init__(self, initial_instrument_context: str = "Ticker: NIFTY (resolved identity)"):
        self._initial_instrument_context = initial_instrument_context
        self.seen_state = None

        class _FakeCompiledGraph:
            def stream(_self, graph_input, **kwargs):
                yield "updates", {"Market Analyst": {}}
                yield "values", {**graph_input, "final_trade_decision": "Rating: Hold"}

        self.graph = _FakeCompiledGraph()

        class _FakePropagator:
            def get_graph_args(_self):
                return {}

        self.propagator = _FakePropagator()

    def create_run_state(self, instrument, as_of_date, asset_type, portfolio):
        return {
            "company_of_interest": instrument, "trade_date": as_of_date,
            "instrument_context": self._initial_instrument_context,
            "final_trade_decision": None,
        }

    @contextmanager
    def checkpoint_scope(self, *a, **k):
        yield None

    def checkpoint_input(self, init_state):
        self.seen_state = init_state
        return init_state

    def _log_state(self, *a, **k):
        pass

    def record_decision(self, *a, **k):
        pass

    def clear_checkpoint_on_success(self, *a, **k):
        pass

    def process_signal(self, full_signal):
        return "Hold"


def test_market_data_context_is_appended_to_instrument_context():
    graph = _FakeGraphObj()
    final_state, signal = _run_graph_with_progress(
        graph, "NIFTY", "2026-01-05", "stock", None, None,
        market_data_context="## Market Data (icici_breeze via TIMESCALEDB)\nLast candle: 23300.0",
    )
    assert "Ticker: NIFTY (resolved identity)" in graph.seen_state["instrument_context"]
    assert "## Market Data (icici_breeze via TIMESCALEDB)" in graph.seen_state["instrument_context"]
    assert "Last candle: 23300.0" in graph.seen_state["instrument_context"]


def test_no_market_data_context_leaves_instrument_context_unchanged():
    """US/generic research (or India research with no data available)
    must render EXACTLY as it did before Phase 8."""
    graph = _FakeGraphObj()
    _run_graph_with_progress(graph, "AAPL", "2026-01-05", "stock", None, None, market_data_context=None)
    assert graph.seen_state["instrument_context"] == "Ticker: NIFTY (resolved identity)"


def test_empty_string_context_is_treated_as_no_context():
    graph = _FakeGraphObj()
    _run_graph_with_progress(graph, "AAPL", "2026-01-05", "stock", None, None, market_data_context="")
    assert graph.seen_state["instrument_context"] == "Ticker: NIFTY (resolved identity)"


def test_missing_instrument_context_key_never_crashes():
    """Belt-and-suspenders: if a future upstream version ever renamed/
    removed this field, injection must degrade gracefully, never crash
    the research run."""

    class _NoContextGraph(_FakeGraphObj):
        def create_run_state(self, instrument, as_of_date, asset_type, portfolio):
            state = super().create_run_state(instrument, as_of_date, asset_type, portfolio)
            del state["instrument_context"]
            return state

    graph = _NoContextGraph()
    final_state, signal = _run_graph_with_progress(
        graph, "NIFTY", "2026-01-05", "stock", None, None, market_data_context="## Market Data",
    )
    assert "instrument_context" not in graph.seen_state
