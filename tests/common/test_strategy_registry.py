"""Phase 10: StrategyRegistry, exercised with the three real strategy
adapters (DoubleStraddleStrategy / CombinedVwapNiftyStrategy /
VwapAlgoNiftyHedgeStrategy) this phase registers, plus one throwaway test
double to exercise error paths without touching a real adapter."""
from __future__ import annotations

import pytest

from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategies.vwap_algo_nifty_hedge import VwapAlgoNiftyHedgeStrategy
from trading.common.strategy import BaseStrategy, StrategyStatus
from trading.common.strategy_registry import (
    DuplicateStrategyError,
    StrategyRegistry,
    UnknownStrategyError,
)


def _registry_with_all_three() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register(DoubleStraddleStrategy())
    registry.register(CombinedVwapNiftyStrategy())
    registry.register(VwapAlgoNiftyHedgeStrategy())
    return registry


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_all_three_strategies_register_successfully():
    registry = _registry_with_all_three()
    assert set(registry.strategy_ids()) == {
        "DoubleStraddelAlgo", "CombinedVwapNifty", "Vwap_Algo_Nifty_hedge",
    }


def test_register_calls_initialize():
    registry = StrategyRegistry()
    strategy = DoubleStraddleStrategy()
    registry.register(strategy)
    assert strategy.get_status() == StrategyStatus.DISABLED


def test_duplicate_registration_is_rejected():
    registry = _registry_with_all_three()
    with pytest.raises(DuplicateStrategyError):
        registry.register(DoubleStraddleStrategy())


def test_unregister_removes_a_strategy():
    registry = _registry_with_all_three()
    registry.unregister("DoubleStraddelAlgo")
    assert "DoubleStraddelAlgo" not in registry.strategy_ids()


def test_unknown_strategy_lookup_raises():
    registry = _registry_with_all_three()
    with pytest.raises(UnknownStrategyError):
        registry.get("NoSuchStrategy")


def test_is_registered():
    registry = _registry_with_all_three()
    assert registry.is_registered("CombinedVwapNifty") is True
    assert registry.is_registered("NoSuchStrategy") is False


# --------------------------------------------------------------------------- #
# Lifecycle control via the registry
# --------------------------------------------------------------------------- #
def test_enable_start_stop_through_the_registry():
    registry = _registry_with_all_three()
    registry.enable("DoubleStraddelAlgo")
    assert registry.get_status("DoubleStraddelAlgo") == StrategyStatus.ENABLED

    registry.start("DoubleStraddelAlgo")
    assert registry.get_status("DoubleStraddelAlgo") == StrategyStatus.SHADOW  # default execution_mode

    registry.stop("DoubleStraddelAlgo")
    assert registry.get_status("DoubleStraddelAlgo") == StrategyStatus.STOPPED


def test_start_all_only_starts_enabled_strategies():
    registry = _registry_with_all_three()
    registry.enable("DoubleStraddelAlgo")
    registry.enable("CombinedVwapNifty")
    # Vwap_Algo_Nifty_hedge left DISABLED on purpose

    registry.start_all()

    assert registry.get_status("DoubleStraddelAlgo") == StrategyStatus.SHADOW
    assert registry.get_status("CombinedVwapNifty") == StrategyStatus.SHADOW
    assert registry.get_status("Vwap_Algo_Nifty_hedge") == StrategyStatus.DISABLED


def test_stop_all_only_stops_active_strategies():
    registry = _registry_with_all_three()
    for sid in registry.strategy_ids():
        registry.enable(sid)
    registry.start_all()

    registry.stop_all()

    assert all(s == StrategyStatus.STOPPED for s in registry.get_all_statuses().values())


def test_get_all_statuses_reports_every_registered_strategy():
    registry = _registry_with_all_three()
    statuses = registry.get_all_statuses()
    assert set(statuses.keys()) == {"DoubleStraddelAlgo", "CombinedVwapNifty", "Vwap_Algo_Nifty_hedge"}
    assert all(v == StrategyStatus.DISABLED for v in statuses.values())


# --------------------------------------------------------------------------- #
# Order-intent generation via the registry
# --------------------------------------------------------------------------- #
def test_generate_order_intents_for_a_single_strategy_returns_a_list():
    registry = _registry_with_all_three()
    registry.enable("DoubleStraddelAlgo")
    registry.start("DoubleStraddelAlgo")

    intents = registry.generate_order_intents("DoubleStraddelAlgo")

    assert isinstance(intents, list)


def test_generate_all_order_intents_skips_inactive_strategies():
    registry = _registry_with_all_three()
    registry.enable("DoubleStraddelAlgo")
    registry.start("DoubleStraddelAlgo")
    # CombinedVwapNifty and Vwap_Algo_Nifty_hedge left DISABLED

    all_intents = registry.generate_all_order_intents()

    assert set(all_intents.keys()) == {"DoubleStraddelAlgo"}


def test_generate_order_intents_for_an_inactive_strategy_raises():
    registry = _registry_with_all_three()
    from trading.common.strategy import InvalidStrategyStateError

    with pytest.raises(InvalidStrategyStateError):
        registry.generate_order_intents("DoubleStraddelAlgo")


# --------------------------------------------------------------------------- #
# get_metrics via the registry
# --------------------------------------------------------------------------- #
def test_get_metrics_reflects_current_status():
    registry = _registry_with_all_three()
    registry.enable("DoubleStraddelAlgo")
    registry.start("DoubleStraddelAlgo")

    metrics = registry.get_metrics("DoubleStraddelAlgo")

    assert metrics.strategy_id == "DoubleStraddelAlgo"
    assert metrics.status == StrategyStatus.SHADOW


def test_get_all_metrics_covers_every_strategy():
    registry = _registry_with_all_three()
    metrics = registry.get_all_metrics()
    assert set(metrics.keys()) == {"DoubleStraddelAlgo", "CombinedVwapNifty", "Vwap_Algo_Nifty_hedge"}


# --------------------------------------------------------------------------- #
# Structural: registry never imports or references anything broker-specific
# --------------------------------------------------------------------------- #
def test_registry_module_has_no_broker_import():
    import trading.common.strategy_registry as mod

    source = open(mod.__file__, encoding="utf-8").read().lower()
    for marker in ("import trading.common.brokers", "smart_api", "breeze_connect", "dhanhq"):
        assert marker not in source


def test_all_three_adapters_are_basestrategy_subclasses():
    registry = _registry_with_all_three()
    for strategy in registry.strategies():
        assert isinstance(strategy, BaseStrategy)
