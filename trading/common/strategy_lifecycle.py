"""
StrategyLifecycle -- Phase 16.3: a read-only PROJECTION of lifecycle state,
derived entirely from existing state (Strategy's own StrategyStatus, Phase
16.2's assignment readiness, and TradingAccount.authorization_state).
Introduces NO new persistence, NO new mutation path, and no relationship to
a human-issued live trading authorization or order execution:

    STRATEGY LIFECYCLE STATE  !=  LIVE AUTHORIZATION  !=  ORDER EXECUTION

This module never imports a broker adapter, never submits/amends/cancels an
order, never imports the separate human-authorized live-canary/authorization
modules, and never constructs an execution engine. It only re-reads state
that already exists.

LifecycleState is a THIRD, deliberately separate vocabulary from both
StrategyStatus (Phase 10 -- ENABLED/DISABLED/STARTING/RUNNING/STOPPED/ERROR/
SHADOW, the strategy's own in-memory administrative/activity flag) and
AccountAuthorizationState (Phase 15B -- DISABLED/READ_ONLY/CANARY_READY/
LIVE_AUTHORIZED/KILLED, how much trust an account's credentials carry).
Neither of those is reused as a lifecycle state, and this module never
merges them into one enum -- it reports all three side by side.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from trading.common.assignment_readiness import check_assignment_readiness
from trading.common.broker_manager import BrokerManager
from trading.common.kill_switch import CentralKillSwitch
from trading.common.strategy import StrategyStatus
from trading.common.strategy_assignment import StrategyAssignment, UnknownAssignmentError
from trading.common.strategy_registry import StrategyRegistry, UnknownStrategyError
from trading.common.trading_account import AccountAuthorizationState

__all__ = [
    "LifecycleState", "StrategyLifecycleView", "check_lifecycle", "check_all_lifecycles",
    "UnknownStrategyError",
]


class LifecycleState(str, Enum):
    """PAUSED is defined for forward compatibility with a possible future
    pause() capability -- the Strategy interface (trading/common/strategy.py)
    has no pause()/resume() method today, and per this phase's explicit
    scope ("do not implement pause/resume if doing so would require
    creating an uncontrolled execution path"), this module never produces
    PAUSED. It exists in the vocabulary, not in any reachable code path."""

    STOPPED = "STOPPED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


# STARTING is transient (BaseStrategy.start() only holds it for the
# duration of one synchronous _on_start() call) -- folded into RUNNING here
# since, from a lifecycle-visibility standpoint, it is already "active".
_ACTIVE_STRATEGY_STATUSES = frozenset(
    {StrategyStatus.RUNNING, StrategyStatus.SHADOW, StrategyStatus.STARTING}
)


@dataclass(frozen=True)
class StrategyLifecycleView:
    strategy_id: str
    # trading/common/strategy_assignment.py's Assignment has no separate
    # assignment_id field (Phase 1/7 design: strategy_id IS the assignment's
    # canonical key, a 1:1 map) -- assignment_id here is strategy_id, not a
    # fabricated second identifier, per this phase's own "do not duplicate
    # assignment data" instruction.
    assignment_id: str | None
    account_id: str | None
    # Phase 16.5: the assignment's own execution_mode (PAPER/SHADOW/LIVE/
    # LIVE_CANARY), "" when no assignment exists. Needed to show a "Mode"
    # column in the TCC lifecycle UI (trading/common/strategy_runtime.py
    # is PAPER/SHADOW-only regardless of what this field reports -- it is
    # purely informational/display, never itself a gate).
    execution_mode: str
    strategy_status: str
    lifecycle_state: LifecycleState
    account_authorization_state: str | None
    live_authorized: bool
    execution_active: bool
    last_transition_at: str
    last_heartbeat_at: str
    last_error: str
    assignment_exists: bool
    blocking_reasons: tuple[str, ...]


def _derive_lifecycle_state(
    strategy_status: StrategyStatus, *, assignment_exists: bool, assignment_valid: bool | None,
) -> LifecycleState:
    if strategy_status in _ACTIVE_STRATEGY_STATUSES:
        # Fail closed: an active-looking strategy with no assignment is an
        # anomaly, never reported as RUNNING (see this phase's brief:
        # "Missing assignment -> ERROR / NOT READY, not RUNNING").
        if not assignment_exists:
            return LifecycleState.ERROR
        return LifecycleState.RUNNING
    if strategy_status == StrategyStatus.ERROR:
        return LifecycleState.ERROR
    if strategy_status == StrategyStatus.ENABLED:
        if assignment_exists and assignment_valid:
            return LifecycleState.READY
        return LifecycleState.STOPPED
    # DISABLED, STOPPED
    return LifecycleState.STOPPED


def check_lifecycle(
    *,
    strategy_registry: StrategyRegistry,
    strategy_assignment: StrategyAssignment,
    broker_manager: BrokerManager,
    kill_switch: CentralKillSwitch,
    strategy_id: str,
) -> StrategyLifecycleView:
    """Raises UnknownStrategyError for an unregistered strategy_id -- exactly
    like every other read endpoint in this codebase. Never raises for a
    missing assignment (that is itself a reportable lifecycle fact, not an
    error condition of this function)."""
    strategy = strategy_registry.get(strategy_id)
    metrics = strategy.get_metrics()
    strategy_status = strategy.get_status()

    assignment_exists = strategy_assignment.has_assignment(strategy_id)
    account_id: str | None = None
    account_authorization_state: str | None = None
    execution_mode = ""
    live_authorized = False
    # "Sound" = the assignment itself is structurally fine (enabled, broker
    # available, account not killed/disabled/insufficiently-authorized) --
    # deliberately NOT the same question as
    # AssignmentReadiness.order_execution_allowed, which also requires the
    # strategy to already be running/shadow and the kill switch to be
    # clear. Those two facts describe EXECUTION readiness, not lifecycle
    # READINESS, and must not leak into whether this strategy is READY.
    assignment_sound: bool | None = None
    blocking_reasons: list[str] = []

    if assignment_exists:
        try:
            readiness = check_assignment_readiness(
                strategy_registry=strategy_registry, strategy_assignment=strategy_assignment,
                broker_manager=broker_manager, kill_switch=kill_switch, strategy_id=strategy_id,
            )
        except UnknownAssignmentError:
            readiness = None  # race: removed between has_assignment() and here
        if readiness is not None:
            account_id = readiness.account_id
            execution_mode = readiness.execution_mode
            assignment_sound = readiness.assignment_valid and readiness.authorization_ok
            # Exclude reasons that describe EXECUTION readiness, not
            # assignment/account soundness -- a strategy that simply hasn't
            # been started yet, or a kill switch that is engaged, must not
            # by itself make a lifecycle-READY assignment look unsound.
            blocking_reasons.extend(
                r for r in readiness.blocking_reasons
                if "not running/shadow" not in r and "kill switch is engaged" not in r
            )
            try:
                account = broker_manager.get_account(readiness.account_id)
                account_authorization_state = account.authorization_state.value
                live_authorized = account.authorization_state == AccountAuthorizationState.LIVE_AUTHORIZED
            except Exception:
                account_authorization_state = None
    else:
        blocking_reasons.append("no assignment exists for this strategy")

    lifecycle_state = _derive_lifecycle_state(
        strategy_status, assignment_exists=assignment_exists, assignment_valid=assignment_sound,
    )
    if lifecycle_state == LifecycleState.ERROR and not assignment_exists and strategy_status in _ACTIVE_STRATEGY_STATUSES:
        blocking_reasons.append(
            f"anomalous: strategy status is {strategy_status.value!r} (active) with no assignment"
        )

    return StrategyLifecycleView(
        strategy_id=strategy_id,
        assignment_id=strategy_id if assignment_exists else None,
        account_id=account_id,
        execution_mode=execution_mode,
        strategy_status=strategy_status.value,
        lifecycle_state=lifecycle_state,
        account_authorization_state=account_authorization_state,
        live_authorized=live_authorized,
        execution_active=lifecycle_state == LifecycleState.RUNNING,
        last_transition_at=metrics.stopped_at or metrics.started_at or "",
        last_heartbeat_at=metrics.last_intent_at,
        last_error=metrics.last_error,
        assignment_exists=assignment_exists,
        blocking_reasons=tuple(blocking_reasons),
    )


def check_all_lifecycles(
    *,
    strategy_registry: StrategyRegistry,
    strategy_assignment: StrategyAssignment,
    broker_manager: BrokerManager,
    kill_switch: CentralKillSwitch,
) -> list[StrategyLifecycleView]:
    return [
        check_lifecycle(
            strategy_registry=strategy_registry, strategy_assignment=strategy_assignment,
            broker_manager=broker_manager, kill_switch=kill_switch, strategy_id=sid,
        )
        for sid in strategy_registry.strategy_ids()
    ]
