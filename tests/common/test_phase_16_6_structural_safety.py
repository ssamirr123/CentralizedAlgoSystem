"""Phase 16.6 structural safety tests -- source-level proof that the new
market-data seam cannot reach a real broker, cannot switch execution to
LIVE, and is never automatically started."""
from __future__ import annotations

import inspect


def test_strategies_never_import_a_broker_adapter_directly():
    import trading.common.strategies.combined_vwap_nifty as combined
    import trading.common.strategies.double_straddle as double
    import trading.common.strategies.vwap_algo_nifty_hedge as vwap
    import trading.common.strategy as strategy_mod

    for mod in (strategy_mod, double, combined, vwap):
        source = inspect.getsource(mod)
        for forbidden in ("AngelOneBroker", "DhanBroker", "ICICIBreezeBroker", "SmartConnect", "breeze_connect"):
            assert forbidden not in source, f"{mod.__name__} must not reference {forbidden}"


def test_market_data_gateway_never_imports_a_broker_adapter():
    import trading.common.market_data_gateway as mod

    source = inspect.getsource(mod)
    for forbidden in ("AngelOneBroker", "DhanBroker", "ICICIBreezeBroker", "PaperBroker", "ShadowBroker"):
        assert forbidden not in source, forbidden


def test_strategy_runtime_never_hardcodes_execution_mode_live():
    import trading.common.strategy_runtime as mod

    source = inspect.getsource(mod)
    assert "ExecutionMode.LIVE" not in source
    assert "ExecutionMode.LIVE_CANARY" not in source


def test_market_data_gateway_has_no_configuration_path_to_execution_mode():
    """The market-data layer must have no notion of execution mode at
    all -- it cannot be configured, directly or indirectly, into
    switching anything to LIVE."""
    import trading.common.market_data_gateway as mod

    source = inspect.getsource(mod)
    assert "ExecutionMode" not in source
    assert "execution_mode" not in source.lower()


def test_app_startup_never_references_the_strategy_runtime():
    import trading.api.app as app_mod

    source = inspect.getsource(app_mod)
    assert "strategy_runtime" not in source
    assert ".run_once(" not in source


def test_execution_state_never_auto_starts_a_strategy():
    """build_execution_state() constructs a StrategyRuntime but never
    calls run_once()/enable()/start() on any strategy -- every strategy
    is registered DISABLED (Phase 10 default) and stays that way until an
    explicit Phase 16.4 START command."""
    from trading.api.execution_state import build_execution_state

    state = build_execution_state()
    for strategy in state.strategy_registry.strategies():
        assert strategy.get_status().value == "disabled"


def test_frontend_lifecycle_page_contains_no_broker_sdk_or_credential_literal():
    import pathlib

    page = pathlib.Path("frontend/src/pages/TccLifecyclePage.tsx").read_text(encoding="utf-8")
    for forbidden in (
        "SmartConnect", "smartapi", "breeze_connect", "BreezeConnect", "dhanhq",
        "api_key", "api_secret", "access_token", "client_secret",
    ):
        assert forbidden not in page, forbidden


def test_two_of_three_registered_strategies_still_declare_no_required_instruments():
    """Documents the current, honest state as of Phase 16.6: no rewrite of
    real strategy decision logic had happened yet -- the market-data gate
    existed and was proven end-to-end via a test-only strategy only.
    Phase 16.7 (see docs/phase-16-7-strategy-order-intent-integration-report.md)
    integrated exactly ONE of the three (CombinedVwapNifty) with real,
    ported decision logic -- the other two remain unchanged, zero-intent,
    and market-data-independent."""
    from trading.common.strategies.double_straddle import DoubleStraddleStrategy
    from trading.common.strategies.vwap_algo_nifty_hedge import VwapAlgoNiftyHedgeStrategy

    for cls in (DoubleStraddleStrategy, VwapAlgoNiftyHedgeStrategy):
        assert cls().required_instruments() == ()

    from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy

    assert CombinedVwapNiftyStrategy().required_instruments() != ()
