"""Phase 16.9 structural safety tests -- source-level proof that no
worker module can reach a real broker, consume a live authorization, or
be started automatically."""
from __future__ import annotations

import inspect

_FORBIDDEN_BROKER_TOKENS = (
    "AngelOneBroker", "DhanBroker", "ICICIBreezeBroker", "SmartConnect", "SmartAPI",
    "breeze_connect", "BreezeConnect", "place_order", "modify_order", "cancel_order",
)

_WORKER_MODULES = (
    "trading.common.worker_identity",
    "trading.common.worker_registry",
    "trading.common.worker_protocol",
    "trading.common.worker_coordinator",
)


def test_no_worker_module_imports_or_calls_a_broker_adapter():
    import importlib

    for name in _WORKER_MODULES:
        mod = importlib.import_module(name)
        source = inspect.getsource(mod)
        for forbidden in _FORBIDDEN_BROKER_TOKENS:
            assert forbidden not in source, f"{name} contains {forbidden!r}"


def test_no_worker_module_consumes_a_live_authorization():
    import importlib

    for name in _WORKER_MODULES:
        mod = importlib.import_module(name)
        source = inspect.getsource(mod)
        assert "live_authorization" not in source.lower(), name
        assert ".consume(" not in source, name


def test_worker_coordinator_never_constructs_a_second_execution_engine():
    import trading.common.worker_coordinator as mod

    source = inspect.getsource(mod)
    assert "StrategyExecutionEngine(" not in source
    assert "PaperBroker(" not in source
    assert "ShadowBroker(" not in source


def test_worker_coordinator_only_ever_calls_the_one_authorized_execution_seam():
    """WorkerCoordinator must reach the broker/execution layer through
    exactly one method -- StrategyRuntime.execute_worker_intent() --
    never through a second, parallel path."""
    import trading.common.worker_coordinator as mod

    source = inspect.getsource(mod)
    assert "execute_worker_intent" in source
    assert ".execute(" not in source  # never calls the engine's execute() directly


def test_application_startup_never_registers_a_worker():
    import trading.api.app as app_mod

    source = inspect.getsource(app_mod)
    assert "worker_coordinator" not in source
    assert "register_worker" not in source


def test_execution_state_never_auto_registers_or_starts_a_worker():
    from trading.api.execution_state import build_execution_state

    state = build_execution_state()
    # build_execution_state() DOES construct an empty WorkerRegistry/
    # WorkerCoordinator (so the read-only /api/workers endpoints have
    # something to read) -- but zero workers are ever pre-registered and
    # no strategy is auto-assigned to one or auto-started, matching every
    # prior phase's "no automatic strategy startup" rule.
    assert state.worker_registry.list_workers() == []
    for strategy in state.strategy_registry.strategies():
        assert strategy.get_status().value == "disabled"
