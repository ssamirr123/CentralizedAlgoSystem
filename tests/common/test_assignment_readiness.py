"""Phase 16.2: trading/common/assignment_readiness.py -- a read-only report
of whether a strategy's current assignment would pass execute()'s own
gates. Never calls a broker, never starts a strategy, never touches
LiveAuthorization."""
from __future__ import annotations

import pytest

from trading.common.assignment_readiness import check_assignment_readiness
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry, UnknownStrategyError
from trading.common.strategy_assignment import UnknownAssignmentError
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount


def _setup(execution_mode=ExecutionMode.SHADOW, authorization_state=AccountAuthorizationState.READ_ONLY):
    manager = BrokerManager()
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper",
        execution_mode=execution_mode, authorization_state=authorization_state,
    )
    manager.register_account(account, broker_client=PaperBroker())
    registry = StrategyRegistry()
    registry.register(DoubleStraddleStrategy())
    assignment = StrategyAssignment(manager)
    kill_switch = CentralKillSwitch()
    return manager, registry, assignment, kill_switch


def _check(registry, assignment, manager, kill_switch, strategy_id="DoubleStraddelAlgo"):
    return check_assignment_readiness(
        strategy_registry=registry, strategy_assignment=assignment,
        broker_manager=manager, kill_switch=kill_switch, strategy_id=strategy_id,
    )


def test_unknown_strategy_raises():
    manager, registry, assignment, kill_switch = _setup()
    with pytest.raises(UnknownStrategyError):
        _check(registry, assignment, manager, kill_switch, strategy_id="NoSuchStrategy")


def test_unassigned_known_strategy_raises():
    manager, registry, assignment, kill_switch = _setup()
    with pytest.raises(UnknownAssignmentError):
        _check(registry, assignment, manager, kill_switch)


def test_blocked_when_strategy_not_started():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign("DoubleStraddelAlgo", "ACC1")
    r = _check(registry, assignment, manager, kill_switch)
    assert r.order_execution_allowed is False
    assert r.assignment_valid is True
    assert r.authorization_ok is True
    assert any("not running/shadow" in reason for reason in r.blocking_reasons)


def test_allowed_once_strategy_is_shadow_running():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign("DoubleStraddelAlgo", "ACC1")
    registry.enable("DoubleStraddelAlgo")
    registry.start("DoubleStraddelAlgo")
    r = _check(registry, assignment, manager, kill_switch)
    assert r.order_execution_allowed is True
    assert r.blocking_reasons == ()
    assert r.broker_capabilities is not None
    assert r.broker_capabilities.broker_type.value == "PAPER"


def test_blocked_when_kill_switch_engaged():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign("DoubleStraddelAlgo", "ACC1")
    registry.enable("DoubleStraddelAlgo")
    registry.start("DoubleStraddelAlgo")
    kill_switch.engage(reason="test", by="tester")
    r = _check(registry, assignment, manager, kill_switch)
    assert r.order_execution_allowed is False
    assert r.kill_switch_engaged is True
    assert any("kill switch" in reason for reason in r.blocking_reasons)


def test_blocked_for_killed_account_even_in_shadow_mode():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign("DoubleStraddelAlgo", "ACC1")
    registry.enable("DoubleStraddelAlgo")
    registry.start("DoubleStraddelAlgo")
    manager.get_account("ACC1").set_killed(reason="test")
    r = _check(registry, assignment, manager, kill_switch)
    assert r.order_execution_allowed is False
    assert r.authorization_ok is False
    assert "KILLED" in r.authorization_detail


def test_read_only_account_is_distinct_from_live_authorized():
    """A READ_ONLY account is sufficient for SHADOW but never for LIVE --
    this readiness report must reflect exactly the same distinction
    execute()'s own gate enforces, without this module adding a new one."""
    manager, registry, assignment, kill_switch = _setup(
        execution_mode=ExecutionMode.LIVE, authorization_state=AccountAuthorizationState.READ_ONLY,
    )
    assignment.assign("DoubleStraddelAlgo", "ACC1", execution_mode=ExecutionMode.LIVE)
    registry.enable("DoubleStraddelAlgo")
    registry.start("DoubleStraddelAlgo")
    r = _check(registry, assignment, manager, kill_switch)
    assert r.authorization_ok is False
    assert r.order_execution_allowed is False


def test_never_calls_place_order_or_constructs_an_execution_engine():
    import inspect
    import trading.common.assignment_readiness as mod

    source = inspect.getsource(mod)
    assert "place_order" not in source
    assert "StrategyExecutionEngine(" not in source
    assert "LiveAuthorization(" not in source


# --------------------------------------------------------------------------- #
# Cross-account isolation (structural regression, Phase 16.2)
# --------------------------------------------------------------------------- #
def test_two_strategies_on_different_accounts_do_not_leak_into_each_other():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="ACC_A", account_name="A", broker_id="paper", enabled=True),
        broker_client=PaperBroker(),
    )
    manager.register_account(
        TradingAccount(account_id="ACC_B", account_name="B", broker_id="paper", enabled=True),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", "ACC_A")
    assignment.assign("StrategyB", "ACC_B")

    assert assignment.get_account_id("StrategyA") == "ACC_A"
    assert assignment.get_account_id("StrategyB") == "ACC_B"

    assignment.remove("StrategyA")
    assert assignment.has_assignment("StrategyA") is False
    assert assignment.has_assignment("StrategyB") is True
    assert assignment.get_account_id("StrategyB") == "ACC_B"
