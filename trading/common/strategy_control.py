"""
StrategyControl -- Phase 16.4: the "Control / Command Service" for the
strategy control plane. Accepts exactly two commands, START and STOP, and
turns each into a deterministic, idempotent, auditable outcome.

    Trading Control Center UI
              |
              v
    Strategy Control API (trading/api/execution_routes.py)
              |
              v
    Control / Command Service (THIS MODULE)
              |
              +-- Assignment validation   (reuses Phase 16.2, unchanged)
              +-- Lifecycle validation    (reuses Phase 16.3, unchanged)
              +-- Account-state validation (via the above)
              +-- Kill-switch validation
              +-- Audit                  (caller writes the audit row --
                                           see the API layer)
              |
              v
    Existing Strategy Control (trading/common/strategy.py -- UNCHANGED)
              |
              v
    Existing Execution Safety Framework -- NEVER reached from here.

This module calls exactly two pre-existing, already-tested Strategy
methods -- enable() and start()/stop() -- and nothing else. It never
generates an OrderIntent, never resolves a broker, never imports a broker
adapter, and never imports anything under the human-authorized
live-canary/authorization modules. START activates the strategy's own
in-memory process/configuration flag (StrategyStatus.RUNNING/SHADOW) --
it never calls generate_order_intents() and never constructs an execution
engine, so `execution_started` below is unconditionally False: this
service cannot, by construction, begin order execution.

    STRATEGY LIFECYCLE STATE  !=  LIVE AUTHORIZATION  !=  ORDER EXECUTION

Idempotency is achieved by INSPECTING current state before acting, not by
a persisted command/idempotency-key store: START while already active, or
STOP while already inactive, is a NOOP -- no second call to
enable()/start()/stop() is made, so there is no possibility of a duplicate
process or lost state. This deliberately reuses the same "smallest safe
change" philosophy as Phase 16.2/16.3 rather than introducing a second,
unrelated idempotency mechanism alongside the existing order-level
idempotency store (trading/common/idempotency_store.py), which solves a
different problem (broker-call replay) and does not fit a lifecycle
command.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum

from trading.common.assignment_readiness import check_assignment_readiness
from trading.common.broker_manager import BrokerManager
from trading.common.kill_switch import CentralKillSwitch
from trading.common.strategy import InvalidStrategyStateError, StrategyStatus
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_lifecycle import LifecycleState, check_lifecycle
from trading.common.strategy_registry import StrategyRegistry, UnknownStrategyError

__all__ = [
    "ControlCommand", "CommandResult", "StrategyControlOutcome",
    "execute_strategy_command", "UnknownStrategyError",
]

_REACTIVATABLE_STATUSES = frozenset({StrategyStatus.DISABLED, StrategyStatus.STOPPED, StrategyStatus.ERROR})
# Mirrors trading/common/strategy.py's own (private) _ACTIVE_STATUSES --
# the exact set BaseStrategy.stop() itself requires to proceed. Used here,
# not lifecycle_state, to decide NOOP-vs-act: lifecycle_state is a
# REPORTING projection (Phase 16.3) and can legitimately read ERROR while
# the strategy is still raw-status RUNNING (the "active with no
# assignment" anomaly) -- a control decision must act on the real,
# authoritative Strategy status, never on a derived report about it.
_ACTIVE_STATUSES = frozenset({StrategyStatus.RUNNING, StrategyStatus.SHADOW})


class ControlCommand(str, Enum):
    """Deliberately exactly these two values -- see this phase's brief:
    do not add BUY/SELL/PLACE_ORDER/AUTHORIZE_LIVE/GO_LIVE here, ever."""

    START = "START"
    STOP = "STOP"


class CommandResult(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NOOP = "NOOP"
    FAILED = "FAILED"


@dataclass(frozen=True)
class StrategyControlOutcome:
    command_id: str
    strategy_id: str
    assignment_id: str | None
    account_id: str | None
    command: ControlCommand
    result: CommandResult
    previous_state: LifecycleState
    new_state: LifecycleState
    accepted: bool
    live_authorized: bool
    execution_started: bool
    reason: str


def _outcome(
    *, strategy_id, assignment_id, account_id, command, result, previous_state, new_state,
    live_authorized, reason,
) -> StrategyControlOutcome:
    return StrategyControlOutcome(
        command_id=str(uuid.uuid4()),
        strategy_id=strategy_id,
        assignment_id=assignment_id,
        account_id=account_id,
        command=command,
        result=result,
        previous_state=previous_state,
        new_state=new_state,
        accepted=result == CommandResult.ACCEPTED,
        live_authorized=live_authorized,
        # Unconditional: this service never generates an OrderIntent and
        # never constructs an execution engine, under any command/result.
        execution_started=False,
        reason=reason,
    )


def execute_strategy_command(
    *,
    strategy_registry: StrategyRegistry,
    strategy_assignment: StrategyAssignment,
    broker_manager: BrokerManager,
    kill_switch: CentralKillSwitch,
    strategy_id: str,
    command: ControlCommand,
) -> StrategyControlOutcome:
    """Raises UnknownStrategyError for an unregistered strategy_id --
    exactly like every other read/write endpoint in this codebase. Never
    raises for any other condition: every other failure mode is reported
    as a REJECTED/NOOP/FAILED outcome, never an exception, so the API layer
    always has something safe and auditable to record."""
    strategy = strategy_registry.get(strategy_id)  # raises UnknownStrategyError
    previous = check_lifecycle(
        strategy_registry=strategy_registry, strategy_assignment=strategy_assignment,
        broker_manager=broker_manager, kill_switch=kill_switch, strategy_id=strategy_id,
    )

    if command == ControlCommand.START:
        return _execute_start(strategy, previous, strategy_registry, strategy_assignment, broker_manager, kill_switch)
    if command == ControlCommand.STOP:
        return _execute_stop(strategy, previous, strategy_registry, strategy_assignment, broker_manager, kill_switch)
    raise ValueError(f"Unsupported control command: {command!r}")  # unreachable given ControlCommand's two members


def _execute_start(strategy, previous, strategy_registry, strategy_assignment, broker_manager, kill_switch) -> StrategyControlOutcome:
    common = dict(
        strategy_id=previous.strategy_id, assignment_id=previous.assignment_id, account_id=previous.account_id,
        command=ControlCommand.START, previous_state=previous.lifecycle_state,
    )

    if strategy.get_status() in _ACTIVE_STATUSES:
        return _outcome(
            **common, result=CommandResult.NOOP, new_state=previous.lifecycle_state,
            live_authorized=previous.live_authorized, reason="strategy is already RUNNING; no action taken",
        )

    if kill_switch.engaged:
        return _outcome(
            **common, result=CommandResult.REJECTED, new_state=previous.lifecycle_state,
            live_authorized=previous.live_authorized, reason="central kill switch is engaged",
        )

    if not previous.assignment_exists:
        return _outcome(
            **common, result=CommandResult.REJECTED, new_state=previous.lifecycle_state,
            live_authorized=False, reason="no assignment exists for this strategy",
        )

    readiness = check_assignment_readiness(
        strategy_registry=strategy_registry, strategy_assignment=strategy_assignment,
        broker_manager=broker_manager, kill_switch=kill_switch, strategy_id=previous.strategy_id,
    )
    if not (readiness.assignment_valid and readiness.authorization_ok):
        reason = "; ".join(readiness.blocking_reasons) or "assignment is not ready for execution"
        return _outcome(
            **common, result=CommandResult.REJECTED, new_state=previous.lifecycle_state,
            live_authorized=readiness.authorization_ok, reason=reason,
        )

    try:
        if strategy.get_status() in _REACTIVATABLE_STATUSES:
            strategy.enable()
        strategy.start()
    except InvalidStrategyStateError as exc:
        return _outcome(
            **common, result=CommandResult.FAILED, new_state=previous.lifecycle_state,
            live_authorized=previous.live_authorized, reason=str(exc),
        )

    new = check_lifecycle(
        strategy_registry=strategy_registry, strategy_assignment=strategy_assignment,
        broker_manager=broker_manager, kill_switch=kill_switch, strategy_id=previous.strategy_id,
    )
    return _outcome(
        **common, result=CommandResult.ACCEPTED, new_state=new.lifecycle_state,
        live_authorized=new.live_authorized, reason="",
    )


def _execute_stop(strategy, previous, strategy_registry, strategy_assignment, broker_manager, kill_switch) -> StrategyControlOutcome:
    common = dict(
        strategy_id=previous.strategy_id, assignment_id=previous.assignment_id, account_id=previous.account_id,
        command=ControlCommand.STOP, previous_state=previous.lifecycle_state,
    )

    # STOP is a pure lifecycle/process control -- it never cancels/closes
    # positions or submits an exit order (see this phase's own safety
    # rule: STOP STRATEGY != CLOSE ALL POSITIONS). It is idempotent from
    # any non-active raw status (DISABLED/ENABLED/STOPPED/ERROR) -- nothing
    # to do. Decided from the strategy's own authoritative status, not the
    # lifecycle_state report (see the module-level comment on
    # _ACTIVE_STATUSES for why).
    if strategy.get_status() not in _ACTIVE_STATUSES:
        return _outcome(
            **common, result=CommandResult.NOOP, new_state=previous.lifecycle_state,
            live_authorized=previous.live_authorized,
            reason=f"strategy is not active (status={strategy.get_status().value!r}); no action taken",
        )

    try:
        strategy.stop()
    except InvalidStrategyStateError as exc:
        return _outcome(
            **common, result=CommandResult.FAILED, new_state=previous.lifecycle_state,
            live_authorized=previous.live_authorized, reason=str(exc),
        )

    new = check_lifecycle(
        strategy_registry=strategy_registry, strategy_assignment=strategy_assignment,
        broker_manager=broker_manager, kill_switch=kill_switch, strategy_id=previous.strategy_id,
    )
    return _outcome(
        **common, result=CommandResult.ACCEPTED, new_state=new.lifecycle_state,
        live_authorized=new.live_authorized, reason="",
    )
