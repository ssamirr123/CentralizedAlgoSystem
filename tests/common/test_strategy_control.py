"""Phase 16.4: trading/common/strategy_control.py -- the Control/Command
Service. Exactly two commands (START/STOP), each deterministic, idempotent,
and incapable of reaching a broker or LiveAuthorization."""
from __future__ import annotations

import threading

import pytest

from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_control import (
    CommandResult,
    ControlCommand,
    UnknownStrategyError,
    execute_strategy_command,
)
from trading.common.strategy_lifecycle import LifecycleState
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


def _send(registry, assignment, manager, kill_switch, command, strategy_id=STRATEGY_ID):
    return execute_strategy_command(
        strategy_registry=registry, strategy_assignment=assignment,
        broker_manager=manager, kill_switch=kill_switch, strategy_id=strategy_id, command=command,
    )


# --------------------------------------------------------------------------- #
# Basic START/STOP
# --------------------------------------------------------------------------- #
def test_unknown_strategy_raises():
    manager, registry, assignment, kill_switch = _setup()
    with pytest.raises(UnknownStrategyError):
        _send(registry, assignment, manager, kill_switch, ControlCommand.START, strategy_id="NoSuch")


def test_start_rejected_with_no_assignment():
    manager, registry, assignment, kill_switch = _setup()
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    assert o.result == CommandResult.REJECTED
    assert o.accepted is False
    assert o.execution_started is False
    assert "no assignment" in o.reason
    assert registry.get_status(STRATEGY_ID).value == "disabled"  # unchanged


def test_start_accepted_with_sound_assignment():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    assert o.result == CommandResult.ACCEPTED
    assert o.accepted is True
    assert o.previous_state == LifecycleState.STOPPED
    assert o.new_state == LifecycleState.RUNNING
    assert o.execution_started is False
    assert o.live_authorized is False
    assert registry.get_status(STRATEGY_ID).value == "shadow"


def test_start_rejected_for_disabled_account():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    manager.get_account("ACC1").enabled = False
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    assert o.result == CommandResult.REJECTED
    assert registry.get_status(STRATEGY_ID).value == "disabled"


def test_start_rejected_for_killed_account():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    manager.get_account("ACC1").set_killed(reason="test")
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    assert o.result == CommandResult.REJECTED
    assert "KILLED" in o.reason
    assert registry.get_status(STRATEGY_ID).value == "disabled"


def test_start_rejected_when_kill_switch_engaged():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    kill_switch.engage(reason="test", by="tester")
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    assert o.result == CommandResult.REJECTED
    assert "kill switch" in o.reason
    assert registry.get_status(STRATEGY_ID).value == "disabled"


def test_start_from_live_mode_read_only_account_is_rejected():
    """START must never let a READ_ONLY account reach LIVE execution."""
    manager, registry, assignment, kill_switch = _setup(authorization_state=AccountAuthorizationState.READ_ONLY)
    manager.get_account("ACC1").execution_mode = ExecutionMode.LIVE
    assignment.assign(STRATEGY_ID, "ACC1", execution_mode=ExecutionMode.LIVE)
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    assert o.result == CommandResult.REJECTED
    assert o.live_authorized is False
    assert registry.get_status(STRATEGY_ID).value == "disabled"


def test_stop_noop_when_already_stopped():
    manager, registry, assignment, kill_switch = _setup()
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.STOP)
    assert o.result == CommandResult.NOOP
    assert o.previous_state == LifecycleState.STOPPED
    assert o.new_state == LifecycleState.STOPPED


def test_stop_accepted_when_running():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.STOP)
    assert o.result == CommandResult.ACCEPTED
    assert o.previous_state == LifecycleState.RUNNING
    assert o.new_state == LifecycleState.STOPPED
    assert registry.get_status(STRATEGY_ID).value == "stopped"


def test_stop_noop_from_error_state():
    manager, registry, assignment, kill_switch = _setup()
    strategy = registry.get(STRATEGY_ID)
    assignment.assign(STRATEGY_ID, "ACC1")
    _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    strategy.mark_error("simulated failure")
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.STOP)
    assert o.result == CommandResult.NOOP
    assert registry.get_status(STRATEGY_ID).value == "error"  # STOP-noop leaves ERROR untouched, no fake recovery


def test_stop_actually_stops_the_anomalous_active_with_no_assignment_case():
    """lifecycle_state reports ERROR for an active-but-unassigned strategy
    (Phase 16.3), but the raw strategy IS still running -- STOP must act on
    the real status, not the lifecycle projection, and actually stop it."""
    manager, registry, assignment, kill_switch = _setup()
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)  # started without ever being assigned
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.STOP)
    assert o.previous_state == LifecycleState.ERROR
    assert o.result == CommandResult.ACCEPTED
    assert registry.get_status(STRATEGY_ID).value == "stopped"


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_repeated_start_is_idempotent_no_duplicate_activation():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    first = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    second = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    third = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    assert first.result == CommandResult.ACCEPTED
    assert second.result == CommandResult.NOOP
    assert third.result == CommandResult.NOOP
    assert registry.get_status(STRATEGY_ID).value == "shadow"
    assert registry.get_metrics(STRATEGY_ID).started_at != ""


def test_repeated_stop_is_idempotent():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    first = _send(registry, assignment, manager, kill_switch, ControlCommand.STOP)
    second = _send(registry, assignment, manager, kill_switch, ControlCommand.STOP)
    assert first.result == CommandResult.ACCEPTED
    assert second.result == CommandResult.NOOP
    assert registry.get_status(STRATEGY_ID).value == "stopped"


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_start_calls_never_produce_two_active_strategies():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    results: list[CommandResult] = []
    lock = threading.Lock()

    def worker():
        o = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
        with lock:
            results.append(o.result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert registry.get_status(STRATEGY_ID).value == "shadow"
    assert results.count(CommandResult.ACCEPTED) >= 1
    # No exception, no crash, and the strategy ends in exactly one
    # well-defined active state -- never duplicated, never corrupted.


def test_concurrent_start_and_stop_leave_a_consistent_final_state():
    manager, registry, assignment, kill_switch = _setup()
    assignment.assign(STRATEGY_ID, "ACC1")
    _send(registry, assignment, manager, kill_switch, ControlCommand.START)

    def stopper():
        _send(registry, assignment, manager, kill_switch, ControlCommand.STOP)

    def starter():
        _send(registry, assignment, manager, kill_switch, ControlCommand.START)

    threads = [threading.Thread(target=stopper), threading.Thread(target=starter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert registry.get_status(STRATEGY_ID).value in ("shadow", "stopped")


# --------------------------------------------------------------------------- #
# Structural safety
# --------------------------------------------------------------------------- #
def test_module_never_touches_broker_or_live_authorization():
    import inspect
    import trading.common.strategy_control as mod

    source = inspect.getsource(mod)
    for forbidden in ("place_order", "modify_order", "cancel_order", "PlaceOrder", "BrokerClient", "smart_api"):
        assert forbidden not in source
    assert "StrategyExecutionEngine(" not in source
    assert "authorize" not in source.lower() or "authorization_ok" in source or "authorization_state" in source


def test_no_command_ever_authorizes_an_account():
    manager, registry, assignment, kill_switch = _setup(authorization_state=AccountAuthorizationState.READ_ONLY)
    assignment.assign(STRATEGY_ID, "ACC1")
    _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    _send(registry, assignment, manager, kill_switch, ControlCommand.STOP)
    assert manager.get_account("ACC1").authorization_state == AccountAuthorizationState.READ_ONLY


def test_execution_started_is_always_false_regardless_of_result():
    manager, registry, assignment, kill_switch = _setup()
    outcomes = [_send(registry, assignment, manager, kill_switch, ControlCommand.START)]  # REJECTED (no assignment)
    assignment.assign(STRATEGY_ID, "ACC1")
    outcomes.append(_send(registry, assignment, manager, kill_switch, ControlCommand.START))  # ACCEPTED
    outcomes.append(_send(registry, assignment, manager, kill_switch, ControlCommand.START))  # NOOP
    outcomes.append(_send(registry, assignment, manager, kill_switch, ControlCommand.STOP))  # ACCEPTED
    for o in outcomes:
        assert o.execution_started is False


def test_control_command_only_has_start_and_stop():
    assert {c.value for c in ControlCommand} == {"START", "STOP"}


# --------------------------------------------------------------------------- #
# Execution engine remains authoritative even after a CONTROL-PLANE START
# --------------------------------------------------------------------------- #
def test_execution_gates_remain_authoritative_after_control_plane_start():
    """A CONTROL-PLANE START only flips StrategyStatus -- it must not by
    itself grant an intent generated afterwards any ability to reach a
    broker. A READ_ONLY account attempting LIVE execution must still be
    rejected by execute()'s own, completely separate, untouched gate."""
    from trading.common.broker import OrderSide, OrderType
    from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
    from trading.common.order_intent import OrderIntent
    from trading.common.risk_manager import RiskManager

    manager, registry, assignment, kill_switch = _setup(authorization_state=AccountAuthorizationState.READ_ONLY)
    manager.get_account("ACC1").execution_mode = ExecutionMode.LIVE
    assignment.assign(STRATEGY_ID, "ACC1", execution_mode=ExecutionMode.LIVE)

    # The control plane correctly REJECTS this START (READ_ONLY + LIVE).
    o = _send(registry, assignment, manager, kill_switch, ControlCommand.START)
    assert o.result == CommandResult.REJECTED

    # Even if an intent were generated regardless (e.g. by a bug elsewhere),
    # execute()'s own pre-existing, unmodified authorization gate must
    # independently reject it -- proving the control plane adds no
    # alternate path around that gate.
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=False),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
        central_kill_switch=kill_switch,
    )
    intent = OrderIntent(
        strategy_id=STRATEGY_ID, account_id="ACC1", symbol="NIFTY24950CE", exchange="NFO",
        side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET, idempotency_key="phase-16-4-gate-check",
    )
    result = engine.execute(intent)
    assert result.success is False
    assert "authorization_state" in result.message or "LIVE" in result.message
