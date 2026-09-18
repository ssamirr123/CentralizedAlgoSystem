"""
AssignmentReadiness -- Phase 16.2: a read-only diagnostic view answering
"if this strategy's currently-generated intents reached
StrategyExecutionEngine.execute() right now, how far would they get?"

This module NEVER calls a broker, NEVER consumes a LiveAuthorization, NEVER
starts/stops a strategy, and NEVER constructs a StrategyExecutionEngine. It
only re-reads state that already exists (StrategyRegistry, StrategyAssignment,
BrokerManager, CentralKillSwitch, TradingAccount.authorization_state) and
reports what execute()'s own gates would already decide -- it does not add,
remove, or change any gate. In particular, authorization_state compatibility
is checked via execution.check_authorization_state_for_mode(), the exact
same function execute() itself calls, so this report can never disagree
with what execution would actually do.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from trading.common.broker_manager import BrokerManager, BrokerUnavailableError, UnknownAccountError
from trading.common.broker_types import (
    BrokerCapabilities,
    UnsupportedBrokerError,
    broker_type_for_id,
    get_capabilities,
)
from trading.common.execution import check_authorization_state_for_mode
from trading.common.kill_switch import CentralKillSwitch
from trading.common.strategy_assignment import (
    InvalidAssignmentError,
    StrategyAssignment,
    UnknownAssignmentError,
)
from trading.common.strategy_registry import StrategyRegistry, UnknownStrategyError
from trading.common.trading_account import AccountAuthorizationError


@dataclass(frozen=True)
class AssignmentReadiness:
    strategy_id: str
    account_id: str
    execution_mode: str
    strategy_status: str
    assignment_enabled: bool
    assignment_valid: bool
    authorization_ok: bool
    authorization_detail: str
    kill_switch_engaged: bool
    broker_capabilities: BrokerCapabilities | None
    order_execution_allowed: bool
    blocking_reasons: tuple[str, ...] = field(default_factory=tuple)


def check_assignment_readiness(
    *,
    strategy_registry: StrategyRegistry,
    strategy_assignment: StrategyAssignment,
    broker_manager: BrokerManager,
    kill_switch: CentralKillSwitch,
    strategy_id: str,
) -> AssignmentReadiness:
    """Raises UnknownStrategyError / UnknownAssignmentError exactly like the
    existing read endpoints do -- callers map those to 404, same as today."""
    strategy_registry.get(strategy_id)  # raises UnknownStrategyError
    assignment = strategy_assignment.get_assignment(strategy_id)  # raises UnknownAssignmentError

    reasons: list[str] = []

    assignment_valid = True
    try:
        strategy_assignment.validate(strategy_id)
    except (InvalidAssignmentError, BrokerUnavailableError, UnknownAccountError) as exc:
        assignment_valid = False
        reasons.append(str(exc))

    authorization_ok = True
    authorization_detail = "authorization_state permits this execution_mode"
    try:
        account = broker_manager.get_account(assignment.account_id)
        check_authorization_state_for_mode(account, assignment.execution_mode)
    except UnknownAccountError as exc:
        authorization_ok = False
        authorization_detail = str(exc)
        reasons.append(authorization_detail)
    except AccountAuthorizationError as exc:
        authorization_ok = False
        authorization_detail = str(exc)
        reasons.append(authorization_detail)

    capabilities: BrokerCapabilities | None = None
    try:
        account = broker_manager.get_account(assignment.account_id)
        capabilities = get_capabilities(broker_type_for_id(account.broker_id))
    except (UnknownAccountError, UnsupportedBrokerError):
        capabilities = None

    kill_switch_engaged = kill_switch.engaged
    if kill_switch_engaged:
        reasons.append("central kill switch is engaged")

    strategy_status = strategy_registry.get_status(strategy_id).value
    if strategy_status not in ("running", "shadow"):
        reasons.append(f"strategy is not running/shadow (status={strategy_status})")

    if not assignment.enabled:
        reasons.append("assignment.enabled is False")

    order_execution_allowed = (
        assignment_valid
        and authorization_ok
        and not kill_switch_engaged
        and strategy_status in ("running", "shadow")
        and assignment.enabled
    )

    return AssignmentReadiness(
        strategy_id=strategy_id,
        account_id=assignment.account_id,
        execution_mode=assignment.execution_mode.value,
        strategy_status=strategy_status,
        assignment_enabled=assignment.enabled,
        assignment_valid=assignment_valid,
        authorization_ok=authorization_ok,
        authorization_detail=authorization_detail,
        kill_switch_engaged=kill_switch_engaged,
        broker_capabilities=capabilities,
        order_execution_allowed=order_execution_allowed,
        blocking_reasons=tuple(reasons),
    )
