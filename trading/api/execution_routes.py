"""
Phase 11 -- Trading Control Center backend: read/control surface over the
broker-agnostic execution framework (Phases 1-10, trading/common/*).
Phase 13 adds the /api/observability/* routes at the bottom of this file.

    GET    /api/strategies
    GET    /api/strategies/{strategy_id}
    POST   /api/strategies/{strategy_id}/start
    POST   /api/strategies/{strategy_id}/stop

    GET    /api/accounts
    GET    /api/accounts/{account_id}
    GET    /api/brokers

    GET    /api/assignments
    GET    /api/assignments/{strategy_id}
    GET    /api/assignments/{strategy_id}/readiness
    POST   /api/assignments

    GET    /api/strategy-lifecycle
    GET    /api/strategy-lifecycle/{strategy_id}
    POST   /api/strategy-lifecycle/{strategy_id}/command   (Phase 16.4: START | STOP)
    POST   /api/strategy-lifecycle/{strategy_id}/evaluate  (Phase 16.5: one PAPER/SHADOW cycle)

    GET    /api/workers                        (Phase 16.9, read-only)
    GET    /api/workers/{worker_id}
    GET    /api/workers/{worker_id}/strategies

    GET    /api/execution-modes

    GET    /api/risk/status
    GET    /api/risk/limits
    POST   /api/risk/kill-switch

    GET    /api/risk/portfolio                 (Phase 16.10, read-only)
    GET    /api/risk/accounts
    GET    /api/risk/accounts/{account_id}
    GET    /api/risk/strategies
    GET    /api/risk/strategies/{strategy_id}

    GET    /api/execution/orders
    GET    /api/execution/positions
    GET    /api/execution/pnl

    GET    /api/system/status

    GET    /api/observability/metrics
    GET    /api/observability/alerts
    GET    /api/observability/trace/{correlation_id}
    GET    /api/observability/audit
    GET    /api/observability/audit/integrity

    GET    /api/operations/summary             (Phase 16.11, read-only)
    GET    /api/operations/system
    GET    /api/operations/workers
    GET    /api/operations/strategies
    GET    /api/operations/accounts
    GET    /api/operations/executions
    GET    /api/operations/intents
    GET    /api/operations/alerts
    GET    /api/operations/audit

Every route here inherits router-level `Depends(get_principal)` +
`Depends(enforce_rate_limit)` (same pattern as trading/api/routes.py) --
so authentication and rate limiting apply uniformly. Each route layers
`Depends(require_permission(...))` on top for its specific capability:
read endpoints require VIEW; strategy start/stop reuse the existing
START/STOP permissions (same ones trader/operator/admin already hold for
algo control); creating an assignment requires TRADING_CONTROL (Phase 11
treats "reroute a strategy to a different account" as the same class of
heavier configuration change TRADING_CONTROL already covers for
servers/algos); the kill switch requires ADMIN -- the single most
sensitive action this API exposes.

Every mutating route writes an audit.record() row to the SAME audit_log
table the rest of the control-center API already uses -- there is no
separate "audit logs" endpoint in this file; read them via the existing
GET /api/admin/audit (Stage 18), which now also shows Phase 11 actions.

AUDIT LOGS requirement: satisfied by reuse of GET /api/admin/audit,
deliberately not duplicated here (see this module's own note above and
docs/phase-11-control-center-backend-report.md).

Path-collision note: GET /api/positions and GET /api/pnl(/today) already
exist in trading/api/routes.py, serving the CURRENT production
telemetry system (deployed algo processes POSTing real heartbeats/
trades/positions/pnl). Those are a different concept entirely from this
phase's shadow-only, in-memory execution framework, so this file uses
/api/execution/orders|positions|pnl instead of colliding with (or
silently shadowing) the existing, already-live routes. See the module
docstring in trading/api/execution_state.py and the Phase 11 report for
the full rationale.

SAFETY: no route in this file ever calls BrokerManager.get_broker() (the
only method that would connect to a real broker), never places, modifies,
or cancels an order, and never returns a broker credential or
TradingAccount.credential_reference (excluded from every response schema
here as defense-in-depth, even though that field is itself documented as
non-secret). generate_order_intents() is never invoked from any route
either -- strategy start/stop only flips this process's own in-memory
StrategyStatus, matching exactly what Phase 10 built and tested.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from trading.api.deps import Principal, client_ip, enforce_rate_limit, get_db, get_principal, require_permission
from trading.api.execution_state import ExecutionState
from trading.api.security import audit
from trading.api.security.permissions import Permission
from trading.common.assignment_readiness import AssignmentReadiness, check_assignment_readiness
from trading.common.broker_manager import BrokerUnavailableError, UnknownAccountError
from trading.common.broker_types import BrokerCapabilities
from trading.common.operations_snapshot import OperationsSnapshot, build_operations_snapshot
from trading.common.portfolio_risk import PortfolioRiskLimits, PortfolioRiskSnapshot
from trading.common.strategy import InvalidStrategyStateError, StrategyMetrics
from trading.common.strategy_assignment import Assignment, InvalidAssignmentError, UnknownAssignmentError
from trading.common.strategy_control import ControlCommand, StrategyControlOutcome, execute_strategy_command
from trading.common.strategy_lifecycle import StrategyLifecycleView, check_all_lifecycles, check_lifecycle
from trading.common.strategy_runtime import RuntimeCycleResult, RuntimeStatus
from trading.common.worker_registry import StrategyAlreadyOwnedError, UnknownWorkerError
from trading.common.strategy_registry import UnknownStrategyError
from trading.common.trading_account import ExecutionMode, TradingAccount

router = APIRouter(dependencies=[Depends(get_principal), Depends(enforce_rate_limit)])

_VIEW = require_permission(Permission.VIEW)
_START = require_permission(Permission.START)
_STOP = require_permission(Permission.STOP)
_TRADING_CONTROL = require_permission(Permission.TRADING_CONTROL)
_ADMIN = require_permission(Permission.ADMIN)


def _state(request: Request) -> ExecutionState:
    return request.app.state.execution


def _audit(
    db, request: Request, principal: Principal, action: str, target: str | None = None,
    detail: dict | None = None, outcome: str = "success",
) -> None:
    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=action, outcome=outcome,
        target=target, ip=client_ip(request), user_agent=request.headers.get("user-agent"), detail=detail,
    )


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class StrategyOut(BaseModel):
    strategy_id: str
    status: str
    execution_mode: str
    intents_generated: int
    error_count: int
    last_error: str
    started_at: str
    stopped_at: str
    last_intent_at: str

    @classmethod
    def from_strategy(cls, strategy) -> "StrategyOut":
        metrics: StrategyMetrics = strategy.get_metrics()
        return cls(
            strategy_id=strategy.strategy_id,
            status=strategy.get_status().value,
            execution_mode=strategy.execution_mode.value,
            intents_generated=metrics.intents_generated,
            error_count=metrics.error_count,
            last_error=metrics.last_error,
            started_at=metrics.started_at,
            stopped_at=metrics.stopped_at,
            last_intent_at=metrics.last_intent_at,
        )


# Deliberately excludes credential_reference and broker_client -- see
# module docstring's SAFETY note.
class AccountOut(BaseModel):
    account_id: str
    account_name: str
    broker_id: str
    enabled: bool
    connection_state: str
    environment: str
    execution_mode: str
    authorization_state: str

    @classmethod
    def from_account(cls, account: TradingAccount) -> "AccountOut":
        return cls(
            account_id=account.account_id, account_name=account.account_name, broker_id=account.broker_id,
            enabled=account.enabled, connection_state=account.connection_state.value,
            environment=account.environment, execution_mode=account.execution_mode.value,
            authorization_state=account.authorization_state.value,
        )


class BrokerOut(BaseModel):
    broker_id: str
    available: bool
    reason: str
    account_count: int


class AssignmentOut(BaseModel):
    strategy_id: str
    account_id: str
    execution_mode: str
    risk_profile: str
    enabled: bool

    @classmethod
    def from_assignment(cls, a: Assignment) -> "AssignmentOut":
        return cls(
            strategy_id=a.strategy_id, account_id=a.account_id, execution_mode=a.execution_mode.value,
            risk_profile=a.risk_profile, enabled=a.enabled,
        )


class AssignmentIn(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    execution_mode: str | None = None
    risk_profile: str = "default"
    enabled: bool = True
    # Phase 16.2: POST /api/assignments creates a NEW assignment by default
    # and now rejects (409) a strategy_id that is already assigned -- an
    # operator must pass replace=true to knowingly reroute an existing
    # assignment to a different account/mode, rather than silently
    # overwriting one via a duplicate form submission. The underlying
    # StrategyAssignment.assign() Python method is unchanged and remains
    # overwrite-capable for internal/legacy callers that never go through
    # this API (see trading/common/strategy_assignment.py).
    replace: bool = False


class AssignmentReadinessOut(BaseModel):
    strategy_id: str
    account_id: str
    execution_mode: str
    strategy_status: str
    assignment_enabled: bool
    assignment_valid: bool
    authorization_ok: bool
    authorization_detail: str
    kill_switch_engaged: bool
    broker_capabilities: dict[str, Any] | None
    order_execution_allowed: bool
    blocking_reasons: list[str]

    @classmethod
    def from_readiness(cls, r: AssignmentReadiness) -> "AssignmentReadinessOut":
        caps: BrokerCapabilities | None = r.broker_capabilities
        return cls(
            strategy_id=r.strategy_id, account_id=r.account_id, execution_mode=r.execution_mode,
            strategy_status=r.strategy_status, assignment_enabled=r.assignment_enabled,
            assignment_valid=r.assignment_valid, authorization_ok=r.authorization_ok,
            authorization_detail=r.authorization_detail, kill_switch_engaged=r.kill_switch_engaged,
            broker_capabilities=(
                None if caps is None else {
                    "broker_type": caps.broker_type.value,
                    "requires_static_ip": caps.requires_static_ip,
                    "supports_websocket": caps.supports_websocket,
                    "supports_orders": caps.supports_orders,
                    "supports_positions": caps.supports_positions,
                    "supports_funds": caps.supports_funds,
                    "supports_options": caps.supports_options,
                    "supports_market_data": caps.supports_market_data,
                    "supports_live_orders": caps.supports_live_orders,
                }
            ),
            order_execution_allowed=r.order_execution_allowed,
            blocking_reasons=list(r.blocking_reasons),
        )


class StrategyLifecycleOut(BaseModel):
    strategy_id: str
    assignment_id: str | None
    account_id: str | None
    execution_mode: str
    strategy_status: str
    lifecycle_state: str
    account_authorization_state: str | None
    live_authorized: bool
    execution_active: bool
    last_transition_at: str
    last_heartbeat_at: str
    last_error: str
    assignment_exists: bool
    blocking_reasons: list[str]
    # Phase 16.5 -- trading/common/strategy_runtime.py. A FOURTH,
    # deliberately separate status from lifecycle_state/strategy_status/
    # account_authorization_state -- see that module's own docstring.
    runtime_state: str
    last_cycle_at: str
    last_runtime_error: str
    last_result_summary: str
    # Phase 16.6 -- trading/common/market_data_gateway.py. "" when the
    # strategy declares no required_instruments() (every strategy
    # registered today) -- otherwise one of MarketDataStatus's values.
    market_data_status: str
    last_market_data_at: str
    # Phase 16.9 -- trading/common/worker_registry.py. None when no
    # worker currently owns this strategy (the local, in-process
    # evaluation path Phases 16.5-16.8 already use remains fully valid
    # with no worker assigned at all -- workers are an additive, optional
    # placement concept, never a requirement).
    worker_id: str | None
    worker_status: str | None
    worker_last_heartbeat_at: str | None

    @classmethod
    def from_view(
        cls, v: StrategyLifecycleView, runtime: RuntimeStatus | None = None, worker: object | None = None,
    ) -> "StrategyLifecycleOut":
        return cls(
            strategy_id=v.strategy_id, assignment_id=v.assignment_id, account_id=v.account_id,
            execution_mode=v.execution_mode, strategy_status=v.strategy_status, lifecycle_state=v.lifecycle_state.value,
            account_authorization_state=v.account_authorization_state, live_authorized=v.live_authorized,
            execution_active=v.execution_active, last_transition_at=v.last_transition_at,
            last_heartbeat_at=(runtime.last_heartbeat_at if runtime and runtime.last_heartbeat_at else v.last_heartbeat_at),
            last_error=v.last_error, assignment_exists=v.assignment_exists,
            blocking_reasons=list(v.blocking_reasons),
            runtime_state=(runtime.state.value if runtime else "INACTIVE"),
            last_cycle_at=(runtime.last_cycle_at if runtime else ""),
            last_runtime_error=(runtime.last_error if runtime else ""),
            worker_id=(worker.worker_id if worker else None),
            worker_status=(worker.status.value if worker else None),
            worker_last_heartbeat_at=(worker.last_heartbeat_at if worker else None),
            market_data_status=(runtime.market_data_status if runtime else ""),
            last_market_data_at=(runtime.last_market_data_at if runtime else ""),
            last_result_summary=(runtime.last_result_summary if runtime else ""),
        )


class StrategyCommandIn(BaseModel):
    command: str  # "START" | "STOP" -- validated against ControlCommand below
    reason: str = Field(default="", max_length=500)


class StrategyCommandOut(BaseModel):
    command_id: str
    strategy_id: str
    assignment_id: str | None
    account_id: str | None
    command: str
    result: str
    previous_state: str
    new_state: str
    accepted: bool
    live_authorized: bool
    execution_started: bool
    reason: str

    @classmethod
    def from_outcome(cls, o: StrategyControlOutcome) -> "StrategyCommandOut":
        return cls(
            command_id=o.command_id, strategy_id=o.strategy_id, assignment_id=o.assignment_id,
            account_id=o.account_id, command=o.command.value, result=o.result.value,
            previous_state=o.previous_state.value, new_state=o.new_state.value, accepted=o.accepted,
            live_authorized=o.live_authorized, execution_started=o.execution_started, reason=o.reason,
        )


class ExecutionModeOut(BaseModel):
    value: str


class RiskLimitsOut(BaseModel):
    max_order_quantity: int | None
    max_position_quantity: int | None
    max_strategy_exposure: float | None
    max_account_exposure: float | None
    max_daily_loss: float | None
    max_strategy_loss: float | None
    max_order_value: float | None


class KillSwitchOut(BaseModel):
    engaged: bool
    engaged_by: str
    reason: str
    engaged_at: str
    disengaged_at: str


class RiskStatusOut(BaseModel):
    kill_switch: KillSwitchOut
    limits: RiskLimitsOut
    assigned_strategy_count: int


class KillSwitchIn(BaseModel):
    engaged: bool
    reason: str = Field(default="", max_length=500)


# Phase 16.10 -- read-only portfolio-risk schemas. `risk_status` is always
# "HEALTHY" in this phase (no violation is possible from a pure read --
# see PortfolioRiskSnapshot's own docstring on why get_snapshot() is
# side-effect free and never itself evaluates a limit); it is included so
# a future phase can report a real status without a breaking schema
# change. Deliberately excludes any BUY/SELL/PLACE ORDER shape -- see
# module docstring's SAFETY note.
class PortfolioRiskOut(BaseModel):
    timestamp: str
    daily_pnl: float
    gross_exposure: float
    open_orders: int
    orders_today: int
    risk_status: str
    limits: dict[str, float | int | None]

    @classmethod
    def from_snapshot(cls, snapshot: PortfolioRiskSnapshot, limits) -> "PortfolioRiskOut":
        return cls(
            timestamp=snapshot.timestamp, daily_pnl=snapshot.portfolio_daily_pnl,
            gross_exposure=snapshot.portfolio_exposure, open_orders=snapshot.portfolio_open_orders,
            orders_today=snapshot.portfolio_orders_today, risk_status="HEALTHY", limits=limits.__dict__,
        )


class AccountRiskOut(BaseModel):
    account_id: str
    daily_pnl: float
    gross_exposure: float
    open_orders: int
    orders_today: int
    risk_status: str
    limits: dict[str, float | int | None]

    @classmethod
    def from_snapshot(cls, snapshot: PortfolioRiskSnapshot, limits) -> "AccountRiskOut":
        return cls(
            account_id=snapshot.account_id, daily_pnl=snapshot.account_daily_pnl,
            gross_exposure=snapshot.account_exposure, open_orders=snapshot.account_open_orders,
            orders_today=snapshot.account_orders_today, risk_status="HEALTHY", limits=limits.__dict__,
        )


class StrategyRiskOut(BaseModel):
    strategy_id: str
    daily_pnl: float
    gross_exposure: float
    open_orders: int
    orders_today: int
    risk_status: str
    limits: dict[str, float | int | None]

    @classmethod
    def from_snapshot(cls, snapshot: PortfolioRiskSnapshot, limits) -> "StrategyRiskOut":
        return cls(
            strategy_id=snapshot.strategy_id, daily_pnl=snapshot.strategy_daily_pnl,
            gross_exposure=snapshot.strategy_exposure, open_orders=snapshot.strategy_open_orders,
            orders_today=snapshot.strategy_orders_today, risk_status="HEALTHY", limits=limits.__dict__,
        )


class OrderOut(BaseModel):
    order_id: str
    strategy_id: str
    account_id: str
    symbol: str
    side: str
    quantity: int
    status: str


class PositionOut(BaseModel):
    strategy_id: str
    account_id: str
    symbol: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float


class PnlOut(BaseModel):
    total_realized: float
    total_unrealized: float
    per_strategy: dict[str, float]


class SystemStatusOut(BaseModel):
    kill_switch_engaged: bool
    strategy_counts_by_status: dict[str, int]
    account_count: int
    broker_availability: dict[str, bool]
    assigned_strategy_count: int


# --------------------------------------------------------------------------- #
# STRATEGIES
# --------------------------------------------------------------------------- #
@router.get("/strategies", response_model=list[StrategyOut])
def list_strategies(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> list[StrategyOut]:
    return [StrategyOut.from_strategy(s) for s in state.strategy_registry.strategies()]


@router.get("/strategies/{strategy_id}", response_model=StrategyOut)
def get_strategy(strategy_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> StrategyOut:
    try:
        return StrategyOut.from_strategy(state.strategy_registry.get(strategy_id))
    except UnknownStrategyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {strategy_id!r}") from None


@router.post("/strategies/{strategy_id}/start", response_model=StrategyOut)
def start_strategy(
    strategy_id: str, request: Request, db: Session = Depends(get_db),
    state: ExecutionState = Depends(_state), principal: Principal = Depends(_START),
) -> StrategyOut:
    try:
        strategy = state.strategy_registry.get(strategy_id)
    except UnknownStrategyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {strategy_id!r}") from None

    try:
        if strategy.get_status().value in ("disabled", "stopped", "error"):
            strategy.enable()
        strategy.start()
    except InvalidStrategyStateError as exc:
        # Phase 16.11 -- reuses the existing STRATEGY_COMMAND_REJECTED
        # constant (Phase 16.4) rather than inventing a new
        # STRATEGY_START_REJECTED event; this legacy direct-mutation route
        # and the newer /strategy-lifecycle/{id}/command endpoint both now
        # audit a rejection the same way.
        _audit(db, request, principal, audit.STRATEGY_COMMAND_REJECTED, target=f"strategy:{strategy_id}",
               outcome="failure", detail={"command": "START", "reason": str(exc)})
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    _audit(db, request, principal, audit.STRATEGY_STARTED, target=f"strategy:{strategy_id}",
           detail={"execution_mode": strategy.execution_mode.value})
    return StrategyOut.from_strategy(strategy)


@router.post("/strategies/{strategy_id}/stop", response_model=StrategyOut)
def stop_strategy(
    strategy_id: str, request: Request, db: Session = Depends(get_db),
    state: ExecutionState = Depends(_state), principal: Principal = Depends(_STOP),
) -> StrategyOut:
    try:
        strategy = state.strategy_registry.get(strategy_id)
    except UnknownStrategyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {strategy_id!r}") from None

    try:
        strategy.stop()
    except InvalidStrategyStateError as exc:
        _audit(db, request, principal, audit.STRATEGY_COMMAND_REJECTED, target=f"strategy:{strategy_id}",
               outcome="failure", detail={"command": "STOP", "reason": str(exc)})
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    _audit(db, request, principal, audit.STRATEGY_STOPPED, target=f"strategy:{strategy_id}")
    return StrategyOut.from_strategy(strategy)


# --------------------------------------------------------------------------- #
# ACCOUNTS / BROKERS
# --------------------------------------------------------------------------- #
@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> list[AccountOut]:
    return [AccountOut.from_account(a) for a in state.broker_manager.accounts()]


@router.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> AccountOut:
    try:
        return AccountOut.from_account(state.broker_manager.get_account(account_id))
    except UnknownAccountError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such account: {account_id!r}") from None


@router.get("/brokers", response_model=list[BrokerOut])
def list_brokers(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> list[BrokerOut]:
    accounts = state.broker_manager.accounts()
    out = []
    for broker_id in state.broker_manager.broker_ids():
        available, reason = state.broker_manager.broker_status(broker_id)
        count = sum(1 for a in accounts if a.broker_id == broker_id)
        out.append(BrokerOut(broker_id=broker_id, available=available, reason=reason, account_count=count))
    return out


# --------------------------------------------------------------------------- #
# ASSIGNMENTS
# --------------------------------------------------------------------------- #
@router.get("/assignments", response_model=list[AssignmentOut])
def list_assignments(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> list[AssignmentOut]:
    out = []
    for strategy in state.strategy_registry.strategies():
        if state.strategy_assignment.has_assignment(strategy.strategy_id):
            out.append(AssignmentOut.from_assignment(state.strategy_assignment.get_assignment(strategy.strategy_id)))
    return out


@router.get("/assignments/{strategy_id}", response_model=AssignmentOut)
def get_assignment(strategy_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> AssignmentOut:
    try:
        return AssignmentOut.from_assignment(state.strategy_assignment.get_assignment(strategy_id))
    except UnknownAssignmentError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No assignment for strategy: {strategy_id!r}") from None


@router.get("/assignments/{strategy_id}/readiness", response_model=AssignmentReadinessOut)
def get_assignment_readiness(
    strategy_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
) -> AssignmentReadinessOut:
    """Read-only: reports whether this strategy's assignment would currently
    pass execute()'s own gates -- never calls a broker, never constructs an
    execution engine, never consumes a human-issued live authorization. See
    trading/common/assignment_readiness.py."""
    try:
        readiness = check_assignment_readiness(
            strategy_registry=state.strategy_registry, strategy_assignment=state.strategy_assignment,
            broker_manager=state.broker_manager, kill_switch=state.kill_switch, strategy_id=strategy_id,
        )
    except UnknownStrategyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {strategy_id!r}") from None
    except UnknownAssignmentError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No assignment for strategy: {strategy_id!r}") from None
    return AssignmentReadinessOut.from_readiness(readiness)


@router.post("/assignments", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
def create_assignment(
    body: AssignmentIn, request: Request, db: Session = Depends(get_db),
    state: ExecutionState = Depends(_state), principal: Principal = Depends(_TRADING_CONTROL),
) -> AssignmentOut:
    if not state.strategy_registry.is_registered(body.strategy_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {body.strategy_id!r}")
    if not body.replace and state.strategy_assignment.has_assignment(body.strategy_id):
        existing = state.strategy_assignment.get_account_id(body.strategy_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Strategy {body.strategy_id!r} is already assigned to account {existing!r}; "
            "pass replace=true to reassign it.",
        )
    try:
        execution_mode = ExecutionMode(body.execution_mode) if body.execution_mode else None
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid execution_mode: {body.execution_mode!r}") from None

    try:
        state.strategy_assignment.assign(
            body.strategy_id, body.account_id, execution_mode=execution_mode,
            risk_profile=body.risk_profile, enabled=body.enabled,
        )
    except UnknownAccountError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such account: {body.account_id!r}") from None
    except BrokerUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except InvalidAssignmentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    _audit(db, request, principal, audit.ASSIGNMENT_SET, target=f"strategy:{body.strategy_id}",
           detail={"account_id": body.account_id, "risk_profile": body.risk_profile, "enabled": body.enabled})
    return AssignmentOut.from_assignment(state.strategy_assignment.get_assignment(body.strategy_id))


# --------------------------------------------------------------------------- #
# STRATEGY LIFECYCLE (Phase 16.3, read-only)
#
# LifecycleState is a projection, not a new persisted state machine: it is
# derived on every read from the existing StrategyStatus (Phase 10) and
# Phase 16.2's assignment readiness. Nothing here calls a broker, starts a
# strategy, or consumes a human-issued live trading authorization -- see
# trading/common/strategy_lifecycle.py's own module docstring.
#
#     STRATEGY LIFECYCLE STATE  !=  LIVE AUTHORIZATION  !=  ORDER EXECUTION
# --------------------------------------------------------------------------- #
def _worker_for(state: ExecutionState, strategy_id: str):
    """Phase 16.9: looks up the WorkerInfo (if any) currently owning
    strategy_id, purely for read-only display on the lifecycle
    endpoints. Returns None when no worker registry is configured or no
    worker owns this strategy -- both are valid, unremarkable states."""
    if state.worker_registry is None:
        return None
    owner_id = state.worker_registry.get_strategy_owner(strategy_id)
    if owner_id is None:
        return None
    try:
        return state.worker_registry.get_worker(owner_id)
    except UnknownWorkerError:
        return None


@router.get("/strategy-lifecycle", response_model=list[StrategyLifecycleOut])
def list_strategy_lifecycle(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> list[StrategyLifecycleOut]:
    views = check_all_lifecycles(
        strategy_registry=state.strategy_registry, strategy_assignment=state.strategy_assignment,
        broker_manager=state.broker_manager, kill_switch=state.kill_switch,
    )
    out = []
    for v in views:
        runtime_status = state.strategy_runtime.get_status(v.strategy_id) if state.strategy_runtime else None
        out.append(StrategyLifecycleOut.from_view(v, runtime_status, _worker_for(state, v.strategy_id)))
    return out


@router.get("/strategy-lifecycle/{strategy_id}", response_model=StrategyLifecycleOut)
def get_strategy_lifecycle(
    strategy_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
) -> StrategyLifecycleOut:
    try:
        view = check_lifecycle(
            strategy_registry=state.strategy_registry, strategy_assignment=state.strategy_assignment,
            broker_manager=state.broker_manager, kill_switch=state.kill_switch, strategy_id=strategy_id,
        )
    except UnknownStrategyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {strategy_id!r}") from None
    runtime_status = state.strategy_runtime.get_status(strategy_id) if state.strategy_runtime else None
    return StrategyLifecycleOut.from_view(view, runtime_status, _worker_for(state, strategy_id))


# --------------------------------------------------------------------------- #
# STRATEGY CONTROL PLANE (Phase 16.4)
#
# Exactly two commands exist: START and STOP. Neither ever generates an
# OrderIntent, resolves a broker, or constructs an execution engine -- see
# trading/common/strategy_control.py's own module docstring. This is the
# ONLY control surface for strategy lifecycle mutation added in Phase
# 16.4; the pre-existing POST /api/strategies/{id}/start|stop (Phase 11)
# is left completely unchanged (including its existing 409-on-invalid-
# transition contract, which several pre-existing tests pin) -- this
# endpoint is additive, reusing the exact same underlying Strategy.enable/
# start/stop() primitives, but with idempotent NOOP handling, an explicit
# Phase 16.2 readiness pre-check, and a richer, auditable result.
# --------------------------------------------------------------------------- #
@router.post("/strategy-lifecycle/{strategy_id}/command", response_model=StrategyCommandOut)
def send_strategy_command(
    strategy_id: str, body: StrategyCommandIn, request: Request, db: Session = Depends(get_db),
    state: ExecutionState = Depends(_state), principal: Principal = Depends(get_principal),
) -> StrategyCommandOut:
    try:
        command = ControlCommand(body.command)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unsupported command: {body.command!r}") from None

    required_permission = Permission.START if command == ControlCommand.START else Permission.STOP
    if not principal.has(required_permission):
        _audit(db, request, principal, audit.PERMISSION_DENIED, target=f"{request.method} {request.url.path}",
               outcome="denied")
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {required_permission.value} permission.")

    try:
        outcome = execute_strategy_command(
            strategy_registry=state.strategy_registry, strategy_assignment=state.strategy_assignment,
            broker_manager=state.broker_manager, kill_switch=state.kill_switch,
            strategy_id=strategy_id, command=command,
        )
    except UnknownStrategyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {strategy_id!r}") from None

    _audit_action = {
        "ACCEPTED": audit.STRATEGY_COMMAND_ACCEPTED, "REJECTED": audit.STRATEGY_COMMAND_REJECTED,
        "NOOP": audit.STRATEGY_COMMAND_NOOP, "FAILED": audit.STRATEGY_COMMAND_FAILED,
    }[outcome.result.value]
    _audit_outcome = {
        "ACCEPTED": "success", "REJECTED": "denied", "NOOP": "success", "FAILED": "failed",
    }[outcome.result.value]
    _audit(
        db, request, principal, _audit_action, target=f"strategy:{strategy_id}", outcome=_audit_outcome,
        detail={
            "command_id": outcome.command_id, "command": outcome.command.value,
            "previous_state": outcome.previous_state.value, "new_state": outcome.new_state.value,
            "reason": body.reason or outcome.reason,
        },
    )
    return StrategyCommandOut.from_outcome(outcome)


class ExecutionResultOut(BaseModel):
    success: bool
    order_id: str
    status: str
    message: str
    filled_quantity: int
    account_id: str


class RuntimeCycleOut(BaseModel):
    strategy_id: str
    ticked: bool
    intents_generated: int
    executions: list[ExecutionResultOut]
    error: str


@router.post("/strategy-lifecycle/{strategy_id}/evaluate", response_model=RuntimeCycleOut)
def evaluate_strategy_runtime(
    strategy_id: str, request: Request, db: Session = Depends(get_db),
    state: ExecutionState = Depends(_state), principal: Principal = Depends(_START),
) -> RuntimeCycleOut:
    """Phase 16.5: one explicit, PAPER/SHADOW-only strategy evaluation
    cycle -- see trading/common/strategy_runtime.py. Requires the same
    START permission as the control-plane START command, since (unlike a
    GET) this can generate a simulated execution. Never places, modifies,
    or cancels a real broker order; never consumes a live authorization."""
    if state.strategy_runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Strategy runtime is not configured.")
    try:
        cycle: RuntimeCycleResult = state.strategy_runtime.run_once(strategy_id)
    except UnknownStrategyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {strategy_id!r}") from None

    _audit(
        db, request, principal, audit.STRATEGY_RUNTIME_EVALUATED, target=f"strategy:{strategy_id}",
        outcome=("failed" if cycle.error else "success"),
        detail={
            "ticked": cycle.ticked, "intents_generated": cycle.intents_generated,
            "executions": len(cycle.executions), "error": cycle.error,
        },
    )
    return RuntimeCycleOut(
        strategy_id=cycle.strategy_id, ticked=cycle.ticked, intents_generated=cycle.intents_generated,
        executions=[
            ExecutionResultOut(
                success=r.success, order_id=r.order_id, status=r.status, message=r.message,
                filled_quantity=r.filled_quantity, account_id=r.account_id,
            )
            for r in cycle.executions
        ],
        error=cycle.error,
    )


class WorkerOut(BaseModel):
    worker_id: str
    name: str
    status: str
    assigned_strategy_ids: list[str]
    last_heartbeat_at: str
    started_at: str
    version: str
    git_sha: str
    host_identity: str

    @classmethod
    def from_info(cls, info) -> "WorkerOut":
        return cls(
            worker_id=info.worker_id, name=info.name, status=info.status.value,
            assigned_strategy_ids=list(info.assigned_strategy_ids), last_heartbeat_at=info.last_heartbeat_at,
            started_at=info.started_at, version=info.version, git_sha=info.git_sha,
            host_identity=info.host_identity,
        )


# --------------------------------------------------------------------------- #
# WORKERS (Phase 16.9, read-only)
#
# A worker never executes an order and never holds a broker credential --
# see trading/common/worker_identity.py's and worker_coordinator.py's own
# module docstrings. These endpoints only ever read the existing
# WorkerRegistry; they never register, command, or otherwise mutate a
# worker (there is no local worker-simulation harness wired into the
# shared Control Center API this phase -- see the phase report's Section
# 15 for how the local simulation was actually exercised, entirely
# within the test suite).
# --------------------------------------------------------------------------- #
@router.get("/workers", response_model=list[WorkerOut])
def list_workers(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> list[WorkerOut]:
    if state.worker_registry is None:
        return []
    return [WorkerOut.from_info(w) for w in state.worker_registry.list_workers()]


@router.get("/workers/{worker_id}", response_model=WorkerOut)
def get_worker(worker_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> WorkerOut:
    if state.worker_registry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such worker: {worker_id!r}")
    try:
        return WorkerOut.from_info(state.worker_registry.get_worker(worker_id))
    except UnknownWorkerError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such worker: {worker_id!r}") from None


@router.get("/workers/{worker_id}/strategies", response_model=list[str])
def get_worker_strategies(
    worker_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
) -> list[str]:
    if state.worker_registry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such worker: {worker_id!r}")
    try:
        info = state.worker_registry.get_worker(worker_id)
    except UnknownWorkerError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such worker: {worker_id!r}") from None
    return list(info.assigned_strategy_ids)


class WorkerAssignIn(BaseModel):
    strategy_id: str


@router.post("/workers/{worker_id}/assign", response_model=WorkerOut)
def assign_worker_strategy(
    worker_id: str, body: WorkerAssignIn, request: Request, db: Session = Depends(get_db),
    state: ExecutionState = Depends(_state), principal: Principal = Depends(_TRADING_CONTROL),
) -> WorkerOut:
    """Phase 16.12 -- closes a gap Phase 16.9/16.10/16.11 left open (no
    operator-facing way to assign strategy ownership to a worker; those
    phases' own tests called WorkerRegistry.assign_strategy() directly).
    Gated by TRADING_CONTROL, the same permission /api/assignments already
    requires for the analogous "reroute a strategy" class of change --
    this never starts/stops a strategy or a worker, it only records
    ownership, exactly like WorkerRegistry.assign_strategy() already did
    when called in-process."""
    if state.worker_registry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such worker: {worker_id!r}")
    try:
        state.worker_registry.assign_strategy(body.strategy_id, worker_id)
    except UnknownWorkerError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such worker: {worker_id!r}") from None
    except StrategyAlreadyOwnedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    _audit(db, request, principal, audit.STRATEGY_WORKER_ASSIGNED, target=f"worker:{worker_id}",
           detail={"strategy_id": body.strategy_id})
    return WorkerOut.from_info(state.worker_registry.get_worker(worker_id))


# --------------------------------------------------------------------------- #
# EXECUTION MODES
# --------------------------------------------------------------------------- #
@router.get("/execution-modes", response_model=list[ExecutionModeOut])
def list_execution_modes(_principal: Principal = Depends(_VIEW)) -> list[ExecutionModeOut]:
    return [ExecutionModeOut(value=m.value) for m in ExecutionMode]


# --------------------------------------------------------------------------- #
# RISK
# --------------------------------------------------------------------------- #
@router.get("/risk/status", response_model=RiskStatusOut)
def risk_status(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> RiskStatusOut:
    limits = state.risk_manager.get_limits()
    ks = state.kill_switch
    assigned = sum(
        1 for s in state.strategy_registry.strategies()
        if state.strategy_assignment.has_assignment(s.strategy_id)
    )
    return RiskStatusOut(
        kill_switch=KillSwitchOut(
            engaged=ks.engaged, engaged_by=ks.engaged_by, reason=ks.reason,
            engaged_at=ks.engaged_at, disengaged_at=ks.disengaged_at,
        ),
        limits=RiskLimitsOut(**limits.__dict__),
        assigned_strategy_count=assigned,
    )


@router.get("/risk/limits", response_model=RiskLimitsOut)
def risk_limits(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> RiskLimitsOut:
    return RiskLimitsOut(**state.risk_manager.get_limits().__dict__)


@router.post("/risk/kill-switch", response_model=KillSwitchOut)
def set_kill_switch(
    body: KillSwitchIn, request: Request, db: Session = Depends(get_db),
    state: ExecutionState = Depends(_state), principal: Principal = Depends(_ADMIN),
) -> KillSwitchOut:
    ks = state.kill_switch
    if body.engaged:
        ks.engage(by=principal.actor, reason=body.reason)
        _audit(db, request, principal, audit.KILL_SWITCH_ENGAGED, target="risk:kill-switch", detail={"reason": body.reason})
    else:
        ks.disengage(by=principal.actor)
        _audit(db, request, principal, audit.KILL_SWITCH_DISENGAGED, target="risk:kill-switch")
    # Phase 13: also raise the observability KILL_SWITCH alert (distinct
    # from the audit_log row above -- this one lands in state.alerts /
    # state.audit_trail, the execution-framework's own tamper-evident
    # trail, alongside every order-lifecycle event).
    state.alerts.kill_switch(body.engaged, by=principal.actor, reason=body.reason)
    return KillSwitchOut(
        engaged=ks.engaged, engaged_by=ks.engaged_by, reason=ks.reason,
        engaged_at=ks.engaged_at, disengaged_at=ks.disengaged_at,
    )


# --------------------------------------------------------------------------- #
# PORTFOLIO RISK (Phase 16.10, read-only)
#
# Every route here calls PortfolioRiskManager.get_snapshot() -- a pure,
# side-effect-free read (see that method's own docstring) -- never
# evaluate_and_reserve(), which would reserve exposure/order-count budget
# as a side effect of a GET. No route here can execute, place, modify, or
# cancel an order, and none accepts a body that could bypass the normal
# OrderIntent -> WorkerCoordinator -> PortfolioRiskManager path.
# --------------------------------------------------------------------------- #
@router.get("/risk/portfolio", response_model=PortfolioRiskOut)
def portfolio_risk(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> PortfolioRiskOut:
    if state.portfolio_risk_manager is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "portfolio risk is not configured")
    snapshot = state.portfolio_risk_manager.get_snapshot()
    return PortfolioRiskOut.from_snapshot(snapshot, state.portfolio_risk_manager.get_portfolio_limits())


@router.get("/risk/accounts", response_model=list[AccountRiskOut])
def list_account_risk(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> list[AccountRiskOut]:
    if state.portfolio_risk_manager is None:
        return []
    out = []
    for account in state.broker_manager.accounts():
        snapshot = state.portfolio_risk_manager.get_snapshot(account_id=account.account_id)
        out.append(AccountRiskOut.from_snapshot(snapshot, state.portfolio_risk_manager.get_account_limits(account.account_id)))
    return out


@router.get("/risk/accounts/{account_id}", response_model=AccountRiskOut)
def get_account_risk(
    account_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
) -> AccountRiskOut:
    if state.portfolio_risk_manager is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "portfolio risk is not configured")
    try:
        state.broker_manager.get_account(account_id)
    except UnknownAccountError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such account: {account_id!r}") from None
    snapshot = state.portfolio_risk_manager.get_snapshot(account_id=account_id)
    return AccountRiskOut.from_snapshot(snapshot, state.portfolio_risk_manager.get_account_limits(account_id))


@router.get("/risk/strategies", response_model=list[StrategyRiskOut])
def list_strategy_risk(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> list[StrategyRiskOut]:
    if state.portfolio_risk_manager is None:
        return []
    out = []
    for strategy in state.strategy_registry.strategies():
        snapshot = state.portfolio_risk_manager.get_snapshot(strategy_id=strategy.strategy_id)
        out.append(StrategyRiskOut.from_snapshot(snapshot, state.portfolio_risk_manager.get_strategy_limits(strategy.strategy_id)))
    return out


@router.get("/risk/strategies/{strategy_id}", response_model=StrategyRiskOut)
def get_strategy_risk(
    strategy_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
) -> StrategyRiskOut:
    if state.portfolio_risk_manager is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "portfolio risk is not configured")
    try:
        state.strategy_registry.get(strategy_id)
    except UnknownStrategyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {strategy_id!r}") from None
    snapshot = state.portfolio_risk_manager.get_snapshot(strategy_id=strategy_id)
    return StrategyRiskOut.from_snapshot(snapshot, state.portfolio_risk_manager.get_strategy_limits(strategy_id))


# --------------------------------------------------------------------------- #
# ORDERS / POSITIONS / P&L
#
# No live or shadow order flow is wired to this API yet -- every strategy's
# generate_order_intents() returns [] today (Phase 10's documented,
# deliberate scope), so these three endpoints honestly report empty/zeroed
# results rather than fabricating data. Their schemas are the intended
# real shape for when a future phase wires actual intent flow through
# RiskManager -> StrategyExecutionEngine and records outcomes somewhere
# this API can read. See docs/phase-11-control-center-backend-report.md.
# --------------------------------------------------------------------------- #
@router.get("/execution/orders", response_model=list[OrderOut])
def list_orders(_principal: Principal = Depends(_VIEW)) -> list[OrderOut]:
    return []


@router.get("/execution/positions", response_model=list[PositionOut])
def list_positions(_principal: Principal = Depends(_VIEW)) -> list[PositionOut]:
    return []


@router.get("/execution/pnl", response_model=PnlOut)
def get_pnl(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> PnlOut:
    return PnlOut(total_realized=0.0, total_unrealized=0.0, per_strategy={s.strategy_id: 0.0 for s in state.strategy_registry.strategies()})


# --------------------------------------------------------------------------- #
# SYSTEM STATUS
# --------------------------------------------------------------------------- #
@router.get("/system/status", response_model=SystemStatusOut)
def system_status(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> SystemStatusOut:
    counts: dict[str, int] = {}
    for s in state.strategy_registry.strategies():
        key = s.get_status().value
        counts[key] = counts.get(key, 0) + 1

    availability = {bid: state.broker_manager.broker_status(bid)[0] for bid in state.broker_manager.broker_ids()}
    assigned = sum(
        1 for s in state.strategy_registry.strategies()
        if state.strategy_assignment.has_assignment(s.strategy_id)
    )
    return SystemStatusOut(
        kill_switch_engaged=state.kill_switch.engaged,
        strategy_counts_by_status=counts,
        account_count=len(state.broker_manager.accounts()),
        broker_availability=availability,
        assigned_strategy_count=assigned,
    )


# --------------------------------------------------------------------------- #
# OBSERVABILITY (Phase 13) -- metrics, alerts, and the tamper-evident
# order-lifecycle audit trail. All read-only (VIEW) except nothing here
# ever mutates state -- these endpoints only ever report what
# state.metrics/state.alerts/state.audit_trail already recorded elsewhere
# (strategy start/stop, broker connect/disconnect, kill-switch engage/
# disengage above, and any execute() call in a future phase that wires
# real intent flow through). See docs/phase-13-observability-report.md.
# --------------------------------------------------------------------------- #
class MetricsSnapshotOut(BaseModel):
    counters: dict[str, int]
    gauges: dict[str, float]
    statuses: dict[str, str]
    latencies: dict[str, list[float]]


class AlertOut(BaseModel):
    seq: int
    timestamp: str
    alert_type: str
    severity: str
    message: str
    correlation_id: str
    detail: dict[str, Any]


class AuditRecordOut(BaseModel):
    seq: int
    timestamp: str
    event_type: str
    correlation_id: str
    strategy_id: str
    detail: dict[str, Any]


class AuditIntegrityOut(BaseModel):
    verified: bool
    record_count: int


@router.get("/observability/metrics", response_model=MetricsSnapshotOut)
def get_metrics(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> MetricsSnapshotOut:
    snap = state.metrics.snapshot()
    return MetricsSnapshotOut(counters=snap.counters, gauges=snap.gauges, statuses=snap.statuses, latencies=snap.latencies)


@router.get("/observability/alerts", response_model=list[AlertOut])
def list_alerts(
    state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
    alert_type: str | None = None,
) -> list[AlertOut]:
    items = state.alerts.alerts()
    if alert_type:
        items = [a for a in items if a.alert_type == alert_type]
    return [
        AlertOut(seq=a.seq, timestamp=a.timestamp, alert_type=a.alert_type, severity=a.severity,
                 message=a.message, correlation_id=a.correlation_id, detail=a.detail)
        for a in items
    ]


@router.get("/observability/trace/{correlation_id}", response_model=list[AuditRecordOut])
def get_trace(
    correlation_id: str, state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
) -> list[AuditRecordOut]:
    """The full Strategy -> OrderIntent -> Risk Decision -> Execution ->
    Broker Order -> Fill -> Position -> P&L trace for one correlation_id.
    An empty list is not an error -- it just means nothing has been
    recorded under that id (e.g. it doesn't exist, or nothing has run
    through execute() yet in this process)."""
    records = state.audit_trail.trace(correlation_id)
    return [
        AuditRecordOut(seq=r.seq, timestamp=r.timestamp, event_type=r.event_type,
                       correlation_id=r.correlation_id, strategy_id=r.strategy_id, detail=r.detail)
        for r in records
    ]


@router.get("/observability/audit", response_model=list[AuditRecordOut])
def list_audit_records(
    state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
    limit: int = 200,
) -> list[AuditRecordOut]:
    """The execution framework's own order-lifecycle audit trail --
    distinct from GET /api/admin/audit (Stage 18's user-action audit log).
    Newest first, capped at `limit` (default 200, max 1000)."""
    records = state.audit_trail.records()[-max(1, min(limit, 1000)):]
    return [
        AuditRecordOut(seq=r.seq, timestamp=r.timestamp, event_type=r.event_type,
                       correlation_id=r.correlation_id, strategy_id=r.strategy_id, detail=r.detail)
        for r in reversed(records)
    ]


@router.get("/observability/audit/integrity", response_model=AuditIntegrityOut)
def audit_integrity(state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW)) -> AuditIntegrityOut:
    """Recomputes the hash chain from scratch -- verified=False would mean
    a record was modified, reordered, or deleted after the fact. See
    trading/common/observability.py's AuditTrail docstring for exactly
    what this guarantees and what it doesn't."""
    records = state.audit_trail.records()
    return AuditIntegrityOut(verified=state.audit_trail.verify(), record_count=len(records))


# --------------------------------------------------------------------------- #
# OPERATIONS (Phase 16.11) -- a single, read-only console over EVERY
# authoritative component above (workers, strategies, accounts, portfolio
# risk, kill switch, deployment info) plus the new OperationalAlertStore.
# Every route here is a pure read: it can raise/resolve a diagnostic
# alert (see trading/common/operations_snapshot.py's own module docstring
# for why that is not a trading-state mutation), but it never starts a
# strategy, never places/modifies/cancels an order, never grants
# authorization, and never changes a risk limit. See
# docs/phase-16-11-operations-monitoring-alerts-report.md.
# --------------------------------------------------------------------------- #
class SystemHealthOut(BaseModel):
    ready: bool
    environment: str
    app_version: str
    git_sha: str
    deployment_id: str
    started_at: str
    uptime_seconds: float
    now: str


class WorkerHealthOut(BaseModel):
    worker_id: str
    name: str
    status: str
    session_id: str
    last_heartbeat_at: str
    heartbeat_age_seconds: float | None
    heartbeat_timeout_seconds: float
    assigned_strategy_ids: list[str]
    version: str
    git_sha: str
    host_identity: str
    started_at: str
    version_mismatch: bool


class StrategyHealthOut(BaseModel):
    strategy_id: str
    lifecycle_state: str
    runtime_state: str
    account_authorization_state: str | None
    execution_active: bool
    worker_id: str | None
    worker_status: str | None
    account_id: str | None
    market_data_status: str
    last_market_data_at: str
    last_cycle_at: str
    last_result_summary: str
    last_error: str


class AccountHealthOut(BaseModel):
    account_id: str
    account_name: str
    broker_id: str
    enabled: bool
    authorization_state: str
    execution_mode: str
    assigned_strategy_ids: list[str]
    worker_ids: list[str]


class SafetyOut(BaseModel):
    kill_switch_engaged: bool
    kill_switch_engaged_by: str
    kill_switch_reason: str
    execution_mode_banner: str
    live_trading_disabled: bool
    # Phase 17.2-P: read-only observability for PortfolioRiskManager's
    # restart-recovery readiness -- distinct from process health (a
    # NOT_READY/RECOVERY_REQUIRED backend can still be "up" and answer
    # /api/health while correctly refusing new reservations).
    portfolio_risk_readiness: str
    portfolio_risk_outstanding_reservations: int


class OperationalAlertOut(BaseModel):
    alert_id: str
    code: str
    severity: str
    category: str
    source_type: str
    source_id: str
    message: str
    raised_at: str
    active: bool
    resolved_at: str | None

    @classmethod
    def from_alert(cls, a) -> "OperationalAlertOut":
        return cls(
            alert_id=a.alert_id, code=a.code, severity=a.severity.value, category=a.category,
            source_type=a.source_type, source_id=a.source_id, message=a.message,
            raised_at=a.raised_at, active=a.active, resolved_at=a.resolved_at,
        )


class OperationsSummaryOut(BaseModel):
    generated_at: str
    system: SystemHealthOut
    workers: list[WorkerHealthOut]
    strategies: list[StrategyHealthOut]
    accounts: list[AccountHealthOut]
    portfolio_risk: PortfolioRiskOut
    safety: SafetyOut
    active_alerts: list[OperationalAlertOut]


def _build_snapshot(state: ExecutionState, db: Session) -> OperationsSnapshot:
    db_ok, _ = _check_database_ready(db)
    return build_operations_snapshot(
        strategy_registry=state.strategy_registry, strategy_assignment=state.strategy_assignment,
        broker_manager=state.broker_manager, kill_switch=state.kill_switch,
        strategy_runtime=state.strategy_runtime, worker_registry=state.worker_registry,
        portfolio_risk_manager=state.portfolio_risk_manager, operational_alerts=state.operational_alerts,
        db_ready=db_ok,
    )


def _check_database_ready(db: Session) -> tuple[bool, str]:
    """Reuses the SAME check trading/api/health.py's /api/ready already
    performs -- never a second, conflicting health semantic (Section 4)."""
    try:
        from sqlalchemy import text

        db.execute(text("SELECT 1"))
        return True, "connected"
    except Exception as exc:  # noqa: BLE001 -- readiness must never crash on this
        return False, f"error: {exc.__class__.__name__}"


def _summary_out(state: ExecutionState, snapshot: OperationsSnapshot) -> OperationsSummaryOut:
    portfolio_limits = (
        state.portfolio_risk_manager.get_portfolio_limits() if state.portfolio_risk_manager is not None
        else PortfolioRiskLimits()
    )
    return OperationsSummaryOut(
        generated_at=snapshot.generated_at,
        system=SystemHealthOut(**snapshot.system.__dict__),
        workers=[WorkerHealthOut(**{**w.__dict__, "assigned_strategy_ids": list(w.assigned_strategy_ids)}) for w in snapshot.workers],
        strategies=[StrategyHealthOut(**w.__dict__) for w in snapshot.strategies],
        accounts=[
            AccountHealthOut(**{
                **a.__dict__, "assigned_strategy_ids": list(a.assigned_strategy_ids), "worker_ids": list(a.worker_ids),
            })
            for a in snapshot.accounts
        ],
        portfolio_risk=PortfolioRiskOut.from_snapshot(snapshot.portfolio_risk, portfolio_limits),
        safety=SafetyOut(**snapshot.safety.__dict__),
        active_alerts=[OperationalAlertOut.from_alert(a) for a in snapshot.active_alerts],
    )


@router.get("/operations/summary", response_model=OperationsSummaryOut)
def operations_summary(
    state: ExecutionState = Depends(_state), db: Session = Depends(get_db), _principal: Principal = Depends(_VIEW),
) -> OperationsSummaryOut:
    return _summary_out(state, _build_snapshot(state, db))


@router.get("/operations/system", response_model=SystemHealthOut)
def operations_system(
    state: ExecutionState = Depends(_state), db: Session = Depends(get_db), _principal: Principal = Depends(_VIEW),
) -> SystemHealthOut:
    return _summary_out(state, _build_snapshot(state, db)).system


@router.get("/operations/workers", response_model=list[WorkerHealthOut])
def operations_workers(
    state: ExecutionState = Depends(_state), db: Session = Depends(get_db), _principal: Principal = Depends(_VIEW),
) -> list[WorkerHealthOut]:
    return _summary_out(state, _build_snapshot(state, db)).workers


@router.get("/operations/strategies", response_model=list[StrategyHealthOut])
def operations_strategies(
    state: ExecutionState = Depends(_state), db: Session = Depends(get_db), _principal: Principal = Depends(_VIEW),
) -> list[StrategyHealthOut]:
    return _summary_out(state, _build_snapshot(state, db)).strategies


@router.get("/operations/accounts", response_model=list[AccountHealthOut])
def operations_accounts(
    state: ExecutionState = Depends(_state), db: Session = Depends(get_db), _principal: Principal = Depends(_VIEW),
) -> list[AccountHealthOut]:
    return _summary_out(state, _build_snapshot(state, db)).accounts


@router.get("/operations/alerts", response_model=list[OperationalAlertOut])
def operations_alerts(
    state: ExecutionState = Depends(_state), db: Session = Depends(get_db), _principal: Principal = Depends(_VIEW),
    active_only: bool = True, severity: str | None = None, category: str | None = None,
) -> list[OperationalAlertOut]:
    # Reconcile first (a fresh read reflects current facts), then answer
    # from the store -- this endpoint can also return resolved history
    # when active_only=False, bounded the same way all_alerts() bounds it.
    _build_snapshot(state, db)
    items = state.operational_alerts.active_alerts() if active_only else state.operational_alerts.all_alerts()
    if severity:
        items = [a for a in items if a.severity.value == severity.upper()]
    if category:
        items = [a for a in items if a.category == category.upper()]
    return [OperationalAlertOut.from_alert(a) for a in items]


@router.get("/operations/audit", response_model=list[AuditRecordOut])
def operations_audit(
    state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW),
    limit: int = 200, strategy_id: str | None = None, event_type: str | None = None,
) -> list[AuditRecordOut]:
    """Convenience alias over the SAME audit trail /api/observability/audit
    already reads -- not a second audit mechanism (Section 24: reuse, do
    not duplicate). Adds a `strategy_id` filter, which the underlying
    AuditTrail/PersistentAuditTrail has no dedicated query for today."""
    records = state.audit_trail.records()
    if strategy_id:
        records = [r for r in records if r.strategy_id == strategy_id]
    if event_type:
        records = [r for r in records if r.event_type == event_type]
    records = records[-max(1, min(limit, 1000)):]
    return [
        AuditRecordOut(seq=r.seq, timestamp=r.timestamp, event_type=r.event_type,
                       correlation_id=r.correlation_id, strategy_id=r.strategy_id, detail=r.detail)
        for r in reversed(records)
    ]


@router.get("/operations/intents", response_model=list[AuditRecordOut])
def operations_intents(
    state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW), limit: int = 50,
) -> list[AuditRecordOut]:
    """Bounded, read-only, recent PORTFOLIO_RISK_DECISION events -- the
    closest thing to "recent OrderIntent processing" this repository's
    existing audit trail can answer without inventing a second, competing
    record of intents (Section 16). Never exposes a broker payload -- the
    detail dict here is exactly what WorkerCoordinator already recorded."""
    records = [r for r in state.audit_trail.records() if r.event_type == "PORTFOLIO_RISK_DECISION"]
    records = records[-max(1, min(limit, 200)):]
    return [
        AuditRecordOut(seq=r.seq, timestamp=r.timestamp, event_type=r.event_type,
                       correlation_id=r.correlation_id, strategy_id=r.strategy_id, detail=r.detail)
        for r in reversed(records)
    ]


@router.get("/operations/executions", response_model=list[AuditRecordOut])
def operations_executions(
    state: ExecutionState = Depends(_state), _principal: Principal = Depends(_VIEW), limit: int = 50,
) -> list[AuditRecordOut]:
    """Bounded, read-only, recent EXECUTION_RESULT events (Section 17) --
    reused from the same existing AuditTrail every StrategyExecutionEngine
    .execute() call already appends to, never a new persistence mechanism."""
    records = [r for r in state.audit_trail.records() if r.event_type == "EXECUTION_RESULT"]
    records = records[-max(1, min(limit, 200)):]
    return [
        AuditRecordOut(seq=r.seq, timestamp=r.timestamp, event_type=r.event_type,
                       correlation_id=r.correlation_id, strategy_id=r.strategy_id, detail=r.detail)
        for r in reversed(records)
    ]
