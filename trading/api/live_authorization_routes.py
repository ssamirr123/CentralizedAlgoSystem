"""
Phase 17.1-R Remediation F -- the FIRST HTTP boundary bridging an
authenticated human operator into `trading.common.live_authorization_workflow`
(Phase 15D.5/15D.6/15D.7, unchanged). Before this phase, the entire
REQUEST -> VALIDATE -> PREFLIGHT -> CONFIRM -> AUTHORIZE chain had only ever
been exercised out-of-band (ad-hoc scripts, tests) -- `trading/common/
operator_identity.py`'s own module docstring named this exact gap as a
"future trading/api route" seam. This module is that route, and nothing
more: it builds an `OperatorIdentity` from the SAME authenticated
`Principal` every other route in this API already trusts, and hands it to
the unchanged workflow layer. It adds NO new execution path, grants NO
live authorization automatically, and never itself calls a broker.

--------------------------------------------------------------------------
Identity boundary (Section 27-29 of this phase's brief)
--------------------------------------------------------------------------
WORKER IDENTITY (`trading.common.worker_auth.WorkerAuthRegistry`,
`X-Worker-Auth`) is a completely separate authentication lane from OPERATOR
identity here -- this router never even imports worker_auth, and its own
structural test proves that. A worker cannot reach these routes with its
own credential, and these routes never accept a `worker_id` in place of an
operator.

The authoritative operator identity is ALWAYS derived from
`request.state.principal` (set by `Depends(get_principal)`/
`Depends(require_permission(...))`, exactly like every other route in this
API) -- via `JwtClaimsAuthenticationProvider.resolve_operator()`, fed a
claims dict built from the Principal's own already-verified fields (never
from the request body). Every Pydantic input schema below sets
`model_config = ConfigDict(extra="forbid")` specifically so a client cannot
even ATTEMPT to smuggle an `operator`/`authorized_by`/`operator_id` field
into the body -- FastAPI/Pydantic rejects the whole request with 422 before
this module's own code ever runs, rather than this module having to
remember to ignore such a field.

--------------------------------------------------------------------------
"Confirmation" (Section 41 -- the human-confirmation boundary)
--------------------------------------------------------------------------
`LiveAuthorizationWorkflow.confirm()` requires a `ConfirmationProvider`
callable representing an explicit "yes" from a human. For this HTTP
surface, the confirmation IS the deliberate, authenticated
`POST /api/live-authorization/{id}/confirm` request itself -- exactly like
clicking a "Confirm" button sends the request that constitutes the
confirmation. This is NOT an automatic bypass: it still requires a valid
session, the `CONFIRM_LIVE_ACTION` permission (checked by
`AuthorizationService.evaluate()` inside `confirm()` itself), a passed
preflight, and one HTTP call an operator must consciously make. There is no
environment variable anywhere in this file (or searched for and confirmed
absent from `trading/common/live_authorization*.py`/`live_canary.py`) that
can substitute for that call.

--------------------------------------------------------------------------
What this module deliberately does NOT do
--------------------------------------------------------------------------
It never starts a strategy, never submits an OrderIntent, never calls
`StrategyExecutionEngine.execute()`, and never calls a broker (`confirm()`'s
own preflight uses `ReadOnlyBrokerView`, read-only by construction -- see
trading/common/reconciliation.py). Creating a LiveAuthorization here grants
NOTHING beyond a durable, single-use, expiring, exactly-scoped row a FUTURE
`execute()` call could try to consume -- exactly as Phase 15D.5 already
built and tested.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from trading.api.deps import Principal, enforce_rate_limit, get_principal, require_permission
from trading.api.execution_state import ExecutionState
from trading.api.security.permissions import Permission
from trading.common.broker_manager import BrokerUnavailableError, UnknownAccountError
from trading.common.live_authorization import DEFAULT_TTL_SECONDS, LiveAuthorizationError
from trading.common.live_authorization_workflow import (
    AuthorizationRequest,
    LiveAuthorizationWorkflow,
    PreflightResult,
    WorkflowError,
    run_preflight,
    validate_request,
)
from trading.common.operator_identity import JwtClaimsAuthenticationProvider, OperatorIdentity

router = APIRouter(dependencies=[Depends(get_principal), Depends(enforce_rate_limit)])

_TRADING_CONTROL = require_permission(Permission.TRADING_CONTROL)

_JWT_PROVIDER = JwtClaimsAuthenticationProvider()


def _state(request: Request) -> ExecutionState:
    return request.app.state.execution


def _operator_from_principal(principal: Principal) -> OperatorIdentity:
    """The ONLY place an OperatorIdentity is constructed in this module --
    always from an already-authenticated Principal (Depends(get_principal)
    has already run and raised 401/403 if not), never from request JSON.
    A service (machine/worker-lane) Principal has no user_id/username and
    is refused here explicitly, even though require_permission would
    already reject most service callers on role grounds -- defense in
    depth against a future service identity that happens to hold
    TRADING_CONTROL."""
    if principal.kind != "user":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Live authorization requires an authenticated human operator, not a service identity")
    claims = {
        "sub": str(principal.user_id), "username": principal.username,
        "role": principal.role, "is_active": True,
    }
    operator = _JWT_PROVIDER.resolve_operator(claims)
    if not operator.is_authenticated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Could not resolve an authenticated operator identity")
    return operator


# --------------------------------------------------------------------------- #
# Schemas -- deliberately NO operator/authorized_by/operator_id field exists
# on any of these; `extra="forbid"` rejects an attempt to smuggle one in.
# --------------------------------------------------------------------------- #
class LiveAuthorizationRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    broker_id: str
    credential_reference: str
    strategy_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    product_type: str
    max_order_value: float
    daily_loss_limit: float
    strategy_loss_limit: float
    max_orders_per_day: int
    idempotency_key: str
    lot_size: int | None = None
    expiry: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS


class LiveAuthorizationOut(BaseModel):
    authorization_id: str
    status: str
    account_id: str
    symbol: str
    quantity: int
    expires_at: str
    authorized_by: str


class LiveAuthorizationConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str


def _to_out(authorization) -> LiveAuthorizationOut:  # noqa: ANN001 -- trading.common.live_authorization.LiveAuthorization
    return LiveAuthorizationOut(
        authorization_id=authorization.authorization_id, status=authorization.status,
        account_id=authorization.account_id, symbol=authorization.symbol, quantity=authorization.quantity,
        expires_at=authorization.expires_at, authorized_by=authorization.authorized_by,
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.post("/live-authorization/request", status_code=status.HTTP_201_CREATED, response_model=LiveAuthorizationOut)
def request_live_authorization(
    body: LiveAuthorizationRequestIn, request: Request,
    principal: Principal = Depends(_TRADING_CONTROL),
) -> LiveAuthorizationOut:
    state = _state(request)
    operator = _operator_from_principal(principal)

    try:
        auth_request = AuthorizationRequest(
            account_id=body.account_id, broker_id=body.broker_id, credential_reference=body.credential_reference,
            symbol=body.symbol, side=body.side, quantity=body.quantity, order_type=body.order_type,
            product_type=body.product_type, max_order_value=body.max_order_value,
            daily_loss_limit=body.daily_loss_limit, strategy_loss_limit=body.strategy_loss_limit,
            max_orders_per_day=body.max_orders_per_day, idempotency_key=body.idempotency_key,
            operator=operator, lot_size=body.lot_size, expiry=body.expiry, ttl_seconds=body.ttl_seconds,
        )
        validate_request(
            auth_request, broker_manager=state.broker_manager, idempotency_store=state.idempotency_store,
            risk_manager=state.risk_manager, strategy_id=body.strategy_id,
            authorization_service=state.authorization_service, audit_trail=state.audit_trail,
        )
    except WorkflowError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    workflow = LiveAuthorizationWorkflow(state.live_authorization_store)
    authorization = workflow.request(auth_request)
    return _to_out(authorization)


@router.post("/live-authorization/{authorization_id}/confirm", response_model=LiveAuthorizationOut)
def confirm_live_authorization(
    authorization_id: str, body: LiveAuthorizationConfirmIn, request: Request,
    principal: Principal = Depends(_TRADING_CONTROL),
) -> LiveAuthorizationOut:
    """The confirming operator may be the same principal who requested it,
    or a different one holding CONFIRM_LIVE_ACTION -- both are durably
    attributed (see this module's own docstring). The HTTP call itself is
    the human confirmation act; see the "Confirmation" section above."""
    state = _state(request)
    operator = _operator_from_principal(principal)

    pending = state.live_authorization_store.get(authorization_id)
    if pending is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such authorization_id={authorization_id!r}")

    try:
        account = state.broker_manager.get_account(pending.account_id)
        broker = state.broker_manager.get_broker(pending.account_id)
    except (UnknownAccountError, BrokerUnavailableError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    try:
        preflight_request = AuthorizationRequest(
            account_id=pending.account_id, broker_id=pending.broker_id, credential_reference=pending.credential_reference,
            symbol=pending.symbol, side=pending.side, quantity=pending.quantity, order_type=pending.order_type,
            product_type=pending.product_type, max_order_value=pending.max_order_value,
            daily_loss_limit=pending.daily_loss_limit, strategy_loss_limit=pending.strategy_loss_limit,
            max_orders_per_day=pending.max_orders_per_day, idempotency_key=pending.idempotency_key,
            operator=operator,  # ttl_seconds is irrelevant here -- only used by AuthorizationRequest.request()/create(), not by run_preflight()
        )
    except WorkflowError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    preflight: PreflightResult = run_preflight(
        preflight_request, broker=broker, risk_manager=state.risk_manager,
        idempotency_store=state.idempotency_store, strategy_id=body.strategy_id,
        central_kill_switch=state.kill_switch,
    )

    workflow = LiveAuthorizationWorkflow(state.live_authorization_store)
    try:
        authorization = workflow.confirm(
            authorization_id, preflight, lambda _preflight: True,
            operator=operator, account=account, authorization_service=state.authorization_service,
            audit_trail=state.audit_trail,
        )
    except WorkflowError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except LiveAuthorizationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _to_out(authorization)


@router.get("/live-authorization/{authorization_id}", response_model=LiveAuthorizationOut)
def get_live_authorization(authorization_id: str, request: Request, principal: Principal = Depends(_TRADING_CONTROL)) -> LiveAuthorizationOut:
    state = _state(request)
    authorization = state.live_authorization_store.get(authorization_id)
    if authorization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such authorization_id={authorization_id!r}")
    return _to_out(authorization)
