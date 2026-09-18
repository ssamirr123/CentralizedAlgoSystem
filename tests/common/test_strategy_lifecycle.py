"""Phase 16.3: trading/common/strategy_lifecycle.py -- a read-only
PROJECTION of lifecycle state (STOPPED/READY/RUNNING/PAUSED/ERROR),
deliberately separate from both StrategyStatus (Phase 10) and
AccountAuthorizationState (Phase 15B). Never calls a broker, never starts a
strategy, never touches LiveAuthorization."""
from __future__ import annotations

import pytest

from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_lifecycle import (
    LifecycleState,
    UnknownStrategyError,
    check_all_lifecycles,
    check_lifecycle,
)
from trading.common.strategy_registry import StrategyRegistry
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount

STRATEGY_ID = "DoubleStraddelAlgo"


def _setup(authorization_state=AccountAuthorizationState.READ_ONLY, account_enabled=True):
    manager = BrokerManager()
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper",
        execution_mode=ExecutionMode.SHADOW, authorization_state=authorization_state, enabled=account_enabled,
    )
    manager.register_account(account, broker_client=PaperBroker())
    registry = StrategyRegistry()
    registry.register(DoubleStraddleStrategy())
    assignment = StrategyAssignment(manager)
    kill_switch = CentralKillSwitch()
    return manager, registry, assignment, kill_switch


def _check(registry, assignment, manager, kill_switch, strategy_id=STRATEGY_ID):
    return check_lifecycle(
        strategy_registry=registry, strategy_assignment=assignment,
        broker_manager=manager, kill_switch=kill_switch, strategy_id=strategy_id,
    )


# --------------------------------------------------------------------------- #
# Basic derivation
# --------------------------------------------------------------------------- #
def test_unknown_strategy_raises():
    manager, registry, assignment, kill_switch = _setup()
    with pytest.raises(UnknownStrategyError):
        _check(registry, assignment, manager, kill_switch, strategy_id="NoSuchStrategy")


def test_initial_state_with_no_assignment_is_stopped():
    manager, registry, assignment, kill_switch = _setup()
    view = _check(registry, assignment, manager, kill_switch)
    assert view.lifecycle_state == LifecycleState.STOPPED
    assert view.strategy_status == "disabled"
    assert view.assignment_exists is False
    assert view.assignment_id is None
    assert view.account_id is None
    assert view.live_authorized is False
    assert view.execution_active is False
    assert "no assignment exists" in view.blocking_reasons[0]


def test_enabled_with_no_assignment_is_stopped_not_ready():
    manager, registry, assignment, kill_switch = _setup()
    registry.enable(STRATEGY_ID)
    view = _check(registry, assignment, manager, kill_switch)
    assert view.lifecycle_state == LifecycleState.STOPPED


def test_enabled_with_valid_assignment_is_ready():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    view = _check(registry, assignment, manager, kill_switch)
    assert view.lifecycle_state == LifecycleState.READY
    assert view.assignment_id == STRATEGY_ID
    assert view.account_id == "ACC1"
    assert view.account_authorization_state == "READ_ONLY"
    assert view.live_authorized is False
    assert view.blocking_reasons == ()


def test_enabled_with_disabled_account_assignment_is_not_ready():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    manager.get_account("ACC1").enabled = False
    view = _check(registry, assignment, manager, kill_switch)
    assert view.lifecycle_state == LifecycleState.STOPPED
    assert len(view.blocking_reasons) > 0


def test_running_strategy_reports_running_and_execution_active():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    view = _check(registry, assignment, manager, kill_switch)
    assert view.strategy_status == "shadow"
    assert view.lifecycle_state == LifecycleState.RUNNING
    assert view.execution_active is True


def test_strategy_error_status_reports_error():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    strategy = registry.get(STRATEGY_ID)
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    strategy.mark_error("simulated failure")
    view = _check(registry, assignment, manager, kill_switch)
    assert view.lifecycle_state == LifecycleState.ERROR
    assert view.last_error == "simulated failure"


def test_active_strategy_with_no_assignment_is_anomalous_error_never_running():
    """Fail-closed per this phase's own rule: a strategy can (today) be
    start()ed without ever being assigned (StrategyRegistry and
    StrategyAssignment are intentionally decoupled) -- this must never be
    reported as RUNNING."""
    manager, registry, assignment, kill_switch = _setup()
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    view = _check(registry, assignment, manager, kill_switch)
    assert view.lifecycle_state == LifecycleState.ERROR
    assert view.execution_active is False
    assert any("anomalous" in r for r in view.blocking_reasons)


# --------------------------------------------------------------------------- #
# Account authorization is a SEPARATE state machine
# --------------------------------------------------------------------------- #
def test_read_only_account_can_be_ready_but_never_live_authorized():
    manager, registry, assignment, kill_switch = _setup(authorization_state=AccountAuthorizationState.READ_ONLY)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    view = _check(registry, assignment, manager, kill_switch)
    assert view.lifecycle_state == LifecycleState.READY
    assert view.account_authorization_state == "READ_ONLY"
    assert view.live_authorized is False


def test_live_authorized_account_still_requires_its_own_lifecycle_state():
    """LIVE_AUTHORIZED on the account must not itself flip lifecycle to
    RUNNING -- the two state machines are independent."""
    manager, registry, assignment, kill_switch = _setup(authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED)
    assignment.assign(STRATEGY_ID, "ACC1")
    view = _check(registry, assignment, manager, kill_switch)  # strategy never enabled
    assert view.lifecycle_state == LifecycleState.STOPPED
    assert view.live_authorized is True
    assert view.execution_active is False


def test_killed_account_assignment_is_not_ready():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    manager.get_account("ACC1").set_killed(reason="test")
    view = _check(registry, assignment, manager, kill_switch)
    assert view.lifecycle_state == LifecycleState.STOPPED
    assert view.account_authorization_state == "KILLED"
    assert any("KILLED" in r for r in view.blocking_reasons)


# --------------------------------------------------------------------------- #
# Cross-account isolation
# --------------------------------------------------------------------------- #
def test_two_strategies_on_different_accounts_report_independent_lifecycles():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="ACC_A", account_name="A", broker_id="paper", enabled=True),
        broker_client=PaperBroker(),
    )
    manager.register_account(
        TradingAccount(account_id="ACC_B", account_name="B", broker_id="paper", enabled=True),
        broker_client=PaperBroker(),
    )
    registry = StrategyRegistry()
    registry.register(DoubleStraddleStrategy())
    from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy

    registry.register(CombinedVwapNiftyStrategy())
    assignment = StrategyAssignment(manager)
    kill_switch = CentralKillSwitch()

    assignment.assign(STRATEGY_ID, "ACC_A")
    assignment.assign("CombinedVwapNifty", "ACC_B")
    registry.enable(STRATEGY_ID)
    registry.enable("CombinedVwapNifty")
    registry.start("CombinedVwapNifty")

    views = {v.strategy_id: v for v in check_all_lifecycles(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager, kill_switch=kill_switch,
    )}
    assert views[STRATEGY_ID].lifecycle_state == LifecycleState.READY
    assert views[STRATEGY_ID].account_id == "ACC_A"
    assert views["CombinedVwapNifty"].lifecycle_state == LifecycleState.RUNNING
    assert views["CombinedVwapNifty"].account_id == "ACC_B"


# --------------------------------------------------------------------------- #
# Structural safety
# --------------------------------------------------------------------------- #
def test_module_never_touches_broker_or_live_authorization():
    import inspect
    import trading.common.strategy_lifecycle as mod

    source = inspect.getsource(mod)
    assert "place_order" not in source
    assert "modify_order" not in source
    assert "cancel_order" not in source
    assert "StrategyExecutionEngine(" not in source
    assert "live_authorization" not in source.lower()


def test_paused_is_defined_but_never_produced():
    """PAUSED is part of the vocabulary for forward-compatibility only --
    this phase implements no pause() capability, so no combination of
    inputs may currently produce it."""
    manager, registry, assignment, kill_switch = _setup()
    for enable in (False, True):
        if enable:
            assignment.assign(STRATEGY_ID, "ACC1")
            registry.enable(STRATEGY_ID)
        view = _check(registry, assignment, manager, kill_switch)
        assert view.lifecycle_state != LifecycleState.PAUSED
