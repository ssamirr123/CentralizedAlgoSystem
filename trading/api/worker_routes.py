"""
Phase 16.12 -- narrowly-scoped MACHINE endpoints for remote strategy
worker processes, over plain HTTPS/REST (see
docs/phase-16-12-distributed-shadow-deployment-validation-report.md
Section 3 for why this transport was chosen over a message broker).

    GET  /api/worker/time                          (unauthenticated clock reference)
    POST /api/worker/register
    POST /api/worker/heartbeat
    POST /api/worker/runtime
    POST /api/worker/order-intents

Every route here is a thin adapter: it authenticates the CALLING WORKER
(via WorkerAuthRegistry -- a distinct lane from human/operator RBAC and
from the existing CONTROL_API_KEY machine lane, see
trading/common/worker_auth.py's own docstring), builds the existing
Phase 16.9 worker_protocol dataclass from the validated request body, and
delegates to the SAME WorkerRegistry/WorkerCoordinator every local
(in-process) test already exercises. NO validation is duplicated here:
every safety check (session match, ownership, lifecycle, staleness,
portfolio risk, RiskManager, kill switch, idempotency, the hard shadow
boundary) lives EXCLUSIVELY in trading/common/worker_registry.py and
worker_coordinator.py, completely unaware this HTTP layer exists.

This file intentionally exposes NOTHING resembling placeOrder/BUY/SELL/
a raw broker request/authorize-live/go-live -- the only mutation surface
is "submit an OrderIntent", which the existing coordinator can only ever
route to a PAPER/ShadowBroker/ConnectedShadowBroker (see
trading/common/strategy_runtime.py's hard shadow boundary, unchanged).

WORKER AUTHENTICATION is a per-worker shared secret (WorkerAuthRegistry),
presented via the `X-Worker-Auth` header on every call. It is checked
BEFORE any WorkerRegistry/WorkerCoordinator call, so an unauthenticated
or wrongly-authenticated caller never reaches (and cannot probe the
existence of) another worker's session/strategy state. The secret is
never echoed back in any response and never logged (route handlers only
ever read it into a local variable passed straight to
WorkerAuthRegistry.verify(), never into a log call or response body).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from trading.api.execution_state import ExecutionState
from trading.common.order_intent import OrderIntent
from trading.common.worker_protocol import OrderIntentSubmission, WorkerHeartbeat, WorkerRegistration
from trading.common.worker_registry import DuplicateWorkerSessionError, StrategyAlreadyOwnedError, UnknownWorkerError

router = APIRouter(prefix="/worker", tags=["worker-transport"])


def _state(request: Request) -> ExecutionState:
    return request.app.state.execution


def _require_worker_auth(worker_id: str, state: ExecutionState, x_worker_auth: str | None) -> None:
    """Fail-closed: an unprovisioned worker_id (WorkerAuthRegistry has no
    secret for it) or a wrong/missing secret both produce the exact same
    401 -- never distinguishing "unknown worker_id" from "wrong secret" in
    the response, which would let a caller enumerate valid worker_ids."""
    if not state.worker_auth_registry.verify(worker_id, x_worker_auth):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "worker authentication failed")


def _require_worker_infra(state: ExecutionState) -> None:
    if state.worker_registry is None or state.worker_coordinator is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "distributed worker transport is not configured")


# --------------------------------------------------------------------------- #
# Schemas -- deliberately separate from worker_protocol.py's own dataclasses
# (which a structural test scans for credential-shaped fields) rather than
# adding an auth-token field to those. The `X-Worker-Auth` header carries
# the secret instead; it never appears in a request/response body schema.
# --------------------------------------------------------------------------- #
class WorkerRegisterIn(BaseModel):
    worker_id: str
    name: str
    version: str = ""
    git_sha: str = ""
    host_identity: str = ""


class WorkerRegisterOut(BaseModel):
    worker_id: str
    session_id: str
    status: str
    heartbeat_timeout_seconds: float
    server_time: str


class WorkerHeartbeatIn(BaseModel):
    worker_id: str
    session_id: str
    strategy_ids: list[str] = Field(default_factory=list)
    runtime_state: str | None = None


class WorkerHeartbeatOut(BaseModel):
    worker_id: str
    status: str
    server_time: str


class WorkerRuntimeIn(BaseModel):
    worker_id: str
    session_id: str
    strategy_id: str
    runtime_state: str
    market_data_status: str = ""
    last_error: str = ""


class WorkerRuntimeOut(BaseModel):
    accepted: bool


class OrderIntentIn(BaseModel):
    worker_id: str
    session_id: str
    strategy_id: str
    evaluation_id: str
    generated_at: str
    submission_id: str
    # OrderIntent's own fields, flattened -- account_id is accepted but
    # (per OrderIntent's and WorkerCoordinator's own long-standing
    # contract) is NEVER trusted for routing; the intent's account_id and
    # the true, centrally-assigned account may legitimately differ, and
    # only the latter is ever used.
    account_id: str
    symbol: str
    exchange: str
    side: str
    quantity: int
    order_type: str = "MARKET"
    limit_price: float | None = None
    trigger_price: float | None = None
    idempotency_key: str = ""
    reason: str = ""


class ExecutionResultOut(BaseModel):
    success: bool
    order_id: str
    status: str
    message: str
    filled_quantity: int
    account_id: str


class OrderIntentOut(BaseModel):
    submission_id: str
    accepted: bool
    reason: str
    execution_result: ExecutionResultOut | None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/time")
def worker_time() -> dict:
    """Unauthenticated clock reference (Section 20) -- a worker can diff
    its own local clock against this to detect obvious drift before it
    ever causes a submission to look stale. Carries no worker/strategy
    state and grants nothing."""
    return {"server_time": datetime.now(timezone.utc).isoformat()}


@router.post("/register", response_model=WorkerRegisterOut)
def worker_register(
    body: WorkerRegisterIn, request: Request, x_worker_auth: str | None = Header(default=None),
) -> WorkerRegisterOut:
    state = _state(request)
    _require_worker_infra(state)
    _require_worker_auth(body.worker_id, state, x_worker_auth)

    registration = WorkerRegistration(
        worker_id=body.worker_id, name=body.name, version=body.version,
        git_sha=body.git_sha, host_identity=body.host_identity,
    )
    try:
        info = state.worker_registry.register_worker(
            worker_id=registration.worker_id, name=registration.name, version=registration.version,
            git_sha=registration.git_sha, host_identity=registration.host_identity,
        )
    except DuplicateWorkerSessionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return WorkerRegisterOut(
        worker_id=info.worker_id, session_id=info.session_id, status=info.status.value,
        heartbeat_timeout_seconds=30.0, server_time=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/heartbeat", response_model=WorkerHeartbeatOut)
def worker_heartbeat(
    body: WorkerHeartbeatIn, request: Request, x_worker_auth: str | None = Header(default=None),
) -> WorkerHeartbeatOut:
    state = _state(request)
    _require_worker_infra(state)
    _require_worker_auth(body.worker_id, state, x_worker_auth)

    heartbeat = WorkerHeartbeat(
        worker_id=body.worker_id, session_id=body.session_id, strategy_ids=tuple(body.strategy_ids),
        runtime_state=body.runtime_state or "",
    )
    try:
        info = state.worker_registry.record_heartbeat(
            heartbeat.worker_id, session_id=heartbeat.session_id,
            strategy_ids=heartbeat.strategy_ids or None, runtime_state=body.runtime_state,
        )
    except UnknownWorkerError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown worker: {body.worker_id!r}") from None
    except DuplicateWorkerSessionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return WorkerHeartbeatOut(worker_id=info.worker_id, status=info.status.value, server_time=datetime.now(timezone.utc).isoformat())


@router.post("/runtime", response_model=WorkerRuntimeOut)
def worker_runtime(
    body: WorkerRuntimeIn, request: Request, x_worker_auth: str | None = Header(default=None),
) -> WorkerRuntimeOut:
    """Observational only -- audits a runtime-status report from a worker
    (Section 16's "runtime update" surface). No central component
    currently gates anything on this beyond the audit record itself; the
    authoritative runtime state remains StrategyRuntime's own bookkeeping,
    updated only by an actual evaluation, never by a worker's self-report."""
    state = _state(request)
    _require_worker_infra(state)
    _require_worker_auth(body.worker_id, state, x_worker_auth)

    try:
        worker = state.worker_registry.get_worker(body.worker_id)
    except UnknownWorkerError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown worker: {body.worker_id!r}") from None
    if worker.session_id != body.session_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "session_id does not match the worker's current active session")

    if state.audit_trail is not None:
        state.audit_trail.append(
            "WORKER_RUNTIME_REPORTED", correlation_id=body.worker_id, strategy_id=body.strategy_id,
            runtime_state=body.runtime_state, market_data_status=body.market_data_status, last_error=body.last_error,
        )
    return WorkerRuntimeOut(accepted=True)


@router.post("/order-intents", response_model=OrderIntentOut)
def worker_order_intent(
    body: OrderIntentIn, request: Request, x_worker_auth: str | None = Header(default=None),
) -> OrderIntentOut:
    state = _state(request)
    _require_worker_infra(state)
    _require_worker_auth(body.worker_id, state, x_worker_auth)

    try:
        intent = OrderIntent(
            strategy_id=body.strategy_id, account_id=body.account_id, symbol=body.symbol, exchange=body.exchange,
            side=body.side, quantity=body.quantity, order_type=body.order_type, limit_price=body.limit_price,
            trigger_price=body.trigger_price, idempotency_key=body.idempotency_key, reason=body.reason,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"malformed OrderIntent: {exc}") from None

    submission = OrderIntentSubmission(
        worker_id=body.worker_id, session_id=body.session_id, strategy_id=body.strategy_id,
        evaluation_id=body.evaluation_id, intent=intent, generated_at=body.generated_at,
        submission_id=body.submission_id,
    )

    try:
        result = state.worker_coordinator.submit_order_intent(submission)
    except StrategyAlreadyOwnedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    execution_out = None
    if result.execution_result is not None:
        r = result.execution_result
        execution_out = ExecutionResultOut(
            success=r.success, order_id=r.order_id, status=r.status, message=r.message,
            filled_quantity=r.filled_quantity, account_id=r.account_id,
        )
    return OrderIntentOut(
        submission_id=result.submission_id, accepted=result.accepted, reason=result.reason,
        execution_result=execution_out,
    )
