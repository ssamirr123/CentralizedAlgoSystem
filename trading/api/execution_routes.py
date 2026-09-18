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
    POST   /api/assignments

    GET    /api/execution-modes

    GET    /api/risk/status
    GET    /api/risk/limits
    POST   /api/risk/kill-switch

    GET    /api/execution/orders
    GET    /api/execution/positions
    GET    /api/execution/pnl

    GET    /api/system/status

    GET    /api/observability/metrics
    GET    /api/observability/alerts
    GET    /api/observability/trace/{correlation_id}
    GET    /api/observability/audit
    GET    /api/observability/audit/integrity

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
from trading.common.broker_manager import BrokerUnavailableError, UnknownAccountError
from trading.common.strategy import InvalidStrategyStateError, StrategyMetrics
from trading.common.strategy_assignment import Assignment, InvalidAssignmentError, UnknownAssignmentError
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


def _audit(db, request: Request, principal: Principal, action: str, target: str | None = None, detail: dict | None = None) -> None:
    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=action,
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

    @classmethod
    def from_account(cls, account: TradingAccount) -> "AccountOut":
        return cls(
            account_id=account.account_id, account_name=account.account_name, broker_id=account.broker_id,
            enabled=account.enabled, connection_state=account.connection_state.value,
            environment=account.environment, execution_mode=account.execution_mode.value,
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


@router.post("/assignments", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
def create_assignment(
    body: AssignmentIn, request: Request, db: Session = Depends(get_db),
    state: ExecutionState = Depends(_state), principal: Principal = Depends(_TRADING_CONTROL),
) -> AssignmentOut:
    if not state.strategy_registry.is_registered(body.strategy_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such strategy: {body.strategy_id!r}")
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
