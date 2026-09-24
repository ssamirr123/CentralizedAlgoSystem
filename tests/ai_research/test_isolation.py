"""
Structural isolation tests for the AI Research Engine (Phase 0/1).

These tests do NOT rely on the `tradingagents` package being installed --
it deliberately isn't part of this project's own requirements.txt (see
requirements-ai-research.txt). Everything here exercises
trading/ai_research/ using only what's already in the backend's normal
test environment.
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

import pytest

AI_RESEARCH_DIR = Path(__file__).resolve().parents[2] / "trading" / "ai_research"

FORBIDDEN_IMPORT_PREFIXES = (
    "trading.common.broker",
    "trading.common.brokers",
    "trading.algos",
)


def _iter_ai_research_source_files():
    # rglob (not glob): Phase 6 added trading/ai_research/backtest/*.py --
    # a recursive scan means any FUTURE subpackage is covered automatically
    # too, with no test-file change needed (the same "no change needed"
    # property Phase 5's own glob-based scan already relied on for its own
    # new files, which a flat glob no longer provides once subpackages exist).
    return sorted(AI_RESEARCH_DIR.rglob("*.py"))


def _imported_module_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_ai_research_package_has_source_files():
    """Sanity check the scan below isn't silently scanning zero files."""
    files = _iter_ai_research_source_files()
    assert len(files) >= 2, f"expected at least __init__.py and config.py, found {files}"


@pytest.mark.parametrize("path", _iter_ai_research_source_files(), ids=lambda p: p.name)
def test_no_execution_capable_imports(path: Path):
    """Section 8/12: trading/ai_research/*.py must never import a broker
    adapter, broker client, or algo/strategy module -- this is the
    structural (not prompt-based) enforcement of the execution boundary."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = _imported_module_names(tree)
    violations = [
        name for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_IMPORT_PREFIXES)
    ]
    assert not violations, f"{path.name} imports forbidden execution-capable module(s): {violations}"


def test_no_tradingagents_import_at_module_level():
    """The adapter must import `tradingagents` lazily (inside a function),
    never at module load time -- so a disabled-flag deployment, or a test
    run without the package installed, never pays that cost and never
    constructs an LLM client just by importing the module."""
    path = AI_RESEARCH_DIR / "trading_agents_adapter.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:  # only TOP-LEVEL statements, not nested in functions
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("tradingagents") for a in node.names), (
                "tradingagents imported at module level"
            )
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("tradingagents"), (
                "tradingagents imported at module level"
            )


def test_adapter_module_imports_cleanly_without_tradingagents_installed():
    """The adapter module itself must be importable even when the
    tradingagents package is absent (as it is in this project's own
    environment) -- proves the lazy-import discipline actually works,
    not just that the source text looks right."""
    assert "tradingagents" not in sys.modules, "test setup assumption violated"
    module = importlib.import_module("trading.ai_research.trading_agents_adapter")
    assert "tradingagents" not in sys.modules, (
        "importing the adapter module must not import tradingagents as a side effect"
    )
    assert hasattr(module, "run_research")


def test_public_surface_is_research_only():
    """Section 12.5: the adapter must expose research functionality only --
    no function/class name resembling order placement, strategy control,
    or account/broker access."""
    module = importlib.import_module("trading.ai_research.trading_agents_adapter")
    public_names = [n for n in dir(module) if not n.startswith("_")]
    forbidden_substrings = (
        "order", "broker", "account", "strategy", "trade_mode", "trading_mode",
        "place", "cancel", "modify", "execute", "start", "stop",
    )
    # Reviewed exceptions: names that happen to contain a forbidden
    # substring but are unambiguously about graph EXECUTION SEQUENCE
    # (node ordering), not broker order placement.
    allowed_exceptions = {"real_node_order"}
    offending = [
        n for n in public_names
        if n not in allowed_exceptions and any(bad in n.lower() for bad in forbidden_substrings)
    ]
    assert not offending, f"adapter exposes execution-shaped names: {offending}"


def test_disabled_flag_prevents_initialization(monkeypatch):
    """Section 1/6/12: with AI_RESEARCH_ENABLED unset (the default),
    run_research() must refuse to run and must never reach the
    tradingagents import at all."""
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    from trading.ai_research.trading_agents_adapter import AIResearchDisabledError, run_research

    assert "tradingagents" not in sys.modules
    with pytest.raises(AIResearchDisabledError):
        run_research("NIFTY", "2026-01-01")
    assert "tradingagents" not in sys.modules, (
        "a disabled run must never import tradingagents"
    )


def test_disabled_flag_explicit_false(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "false")
    from trading.ai_research.trading_agents_adapter import AIResearchDisabledError, run_research

    with pytest.raises(AIResearchDisabledError):
        run_research("NIFTY", "2026-01-01")


def test_enabled_but_package_not_installed_fails_safely(monkeypatch):
    """Section 12.3/12.4: with the flag on but tradingagents absent (as it
    genuinely is in this environment), the adapter must raise its own
    typed error -- not a raw ImportError bubbling up, and it must not
    crash the process or import anything execution-capable in the
    process of failing."""
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    from trading.ai_research.trading_agents_adapter import AIResearchConfigError, run_research

    with pytest.raises(AIResearchConfigError):
        run_research("NIFTY", "2026-01-01")


def test_tradingagents_exception_is_converted_not_raised_raw(monkeypatch):
    """Section 12.4: simulates a real TradingAgents/LLM-side failure (e.g.
    a missing API key) using a fake module injected via sys.modules --
    confirms it is caught and converted to AIResearchConfigError, never
    left to propagate raw or crash the caller."""
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")

    import types

    fake_default_config = types.ModuleType("tradingagents.default_config")
    fake_default_config.DEFAULT_CONFIG = {}

    fake_trading_graph = types.ModuleType("tradingagents.graph.trading_graph")

    class _FakeGraph:
        def __init__(self, *_, **__):
            raise RuntimeError("simulated: no ANTHROPIC_API_KEY / OPENAI_API_KEY set")

    fake_trading_graph.TradingAgentsGraph = _FakeGraph

    fake_portfolio = types.ModuleType("tradingagents.portfolio")

    class _FakePosition:
        def __init__(self, *_, **__):
            pass

    class _FakePortfolioContext:
        def __init__(self, *_, **__):
            pass

    fake_portfolio.Position = _FakePosition
    fake_portfolio.PortfolioContext = _FakePortfolioContext

    fake_tradingagents = types.ModuleType("tradingagents")
    fake_graph_pkg = types.ModuleType("tradingagents.graph")

    monkeypatch.setitem(sys.modules, "tradingagents", fake_tradingagents)
    monkeypatch.setitem(sys.modules, "tradingagents.default_config", fake_default_config)
    monkeypatch.setitem(sys.modules, "tradingagents.graph", fake_graph_pkg)
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", fake_trading_graph)
    monkeypatch.setitem(sys.modules, "tradingagents.portfolio", fake_portfolio)

    from trading.ai_research.trading_agents_adapter import AIResearchConfigError, run_research

    with pytest.raises(AIResearchConfigError, match="simulated"):
        run_research("NIFTY", "2026-01-01")


def test_settings_default_disabled(monkeypatch):
    """Section 6: AI_RESEARCH_ENABLED must default to false when unset,
    regardless of any other environment variable state."""
    monkeypatch.delenv("AI_RESEARCH_ENABLED", raising=False)
    from trading.ai_research.config import load_ai_research_settings

    settings = load_ai_research_settings()
    assert settings.enabled is False


def test_settings_reads_enabled_flag(monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    from trading.ai_research.config import load_ai_research_settings

    settings = load_ai_research_settings()
    assert settings.enabled is True


def test_run_research_signature_accepts_no_execution_parameters():
    """Section 8/10 (Phase 3 scope, extended Phase 8): run_research() now
    accepts real TradingAgents research-configuration parameters (analyst
    selection, LLM/model choice, debate rounds, retry count, data
    vendors, portfolio RESEARCH context, a real per-node completion
    callback, and Phase 8's market_data_context -- an already-fetched,
    already-normalized text block, never a Breeze credential or a
    callable that could reach Breeze) -- but still nothing broker/
    account/order/strategy-shaped, so there is still nothing to pass
    through even if a caller tried."""
    import inspect

    from trading.ai_research.trading_agents_adapter import run_research

    params = set(inspect.signature(run_research).parameters)
    forbidden = {
        "broker", "account", "account_id", "quantity", "order_type",
        "side", "live", "paper", "strategy_id", "trading_mode",
    }
    assert not (params & forbidden), f"run_research accepts execution-shaped parameter(s): {params & forbidden}"
    expected = {
        "instrument", "as_of_date", "selected_analysts", "llm_provider",
        "deep_think_llm", "quick_think_llm", "temperature", "debate_rounds",
        "risk_debate_rounds", "retry_count", "data_vendors", "checkpoint_enabled",
        "portfolio", "on_node_complete", "market_data_context",
    }
    assert params == expected, f"unexpected parameter set: {params.symmetric_difference(expected)}"
