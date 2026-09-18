"""
Phase 15D.6 -- the operator-facing workflow around Phase 15D.5's
`LiveAuthorization` mechanism:

    REQUEST -> VALIDATE -> PREFLIGHT -> HUMAN CONFIRMATION -> AUTHORIZE -> EXECUTE

This module builds NO new execution path and NO second authorization
mechanism. It orchestrates `trading.common.live_authorization` (Phase
15D.5, unchanged) and `trading.common.execution.StrategyExecutionEngine`
(unchanged) into a deterministic, fully-testable sequence. Every existing
gate (kill switch, RiskManager, LiveCanaryGuard, idempotency,
LiveAuthorization itself) remains exactly where it already was; this
workflow only adds REQUEST-time and PREFLIGHT-time validation plus an
explicit human-confirmation step strictly BEFORE any of that pipeline is
ever reached -- never after, never instead of.

Human confirmation is represented as a narrow callable
(`ConfirmationProvider`) taking a `PreflightResult` and returning a bool.
Only a return value of exactly `True` counts as approval -- missing
input, a timeout, invalid input, arbitrary text, an exception, or any
other value is treated as NOT approved. Building a real interactive
confirmation surface (terminal prompt, web UI, chat exchange) is
deliberately out of scope.

--------------------------------------------------------------------------
Phase 15D.7 -- operator identity, not free text
--------------------------------------------------------------------------
Phase 15D.6 accepted `authorized_by` as a caller-supplied string, checked
only for non-emptiness and not looking secret-shaped. Phase 15D.7 removes
that trust: `AuthorizationRequest` now requires an authenticated
`OperatorIdentity` (trading.common.operator_identity) instead of a name,
`authorized_by` is DERIVED from it (never settable independently), and
both `validate_request()` and `LiveAuthorizationWorkflow.confirm()`
consult an `AuthorizationService` (trading.common.live_authorization_service)
to fail closed on an unauthenticated, disabled, under-permissioned, or
wrong-account-ownership operator -- see this phase's own
docs/phase-15d-7-implementation-plan.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from trading.common.audit_store import redact_string
from trading.common.broker import BrokerClient, OrderSide, OrderType
from trading.common.broker_manager import BrokerManager, UnknownAccountError
from trading.common.idempotency_store import IdempotencyStore
from trading.common.live_authorization import (
    DEFAULT_TTL_SECONDS,
    LiveAuthorization,
    LiveAuthorizationError,
    SqliteLiveAuthorizationStore,
    mask_credential_reference,
)
from trading.common.live_authorization_service import AuthorizationService
from trading.common.live_canary import CanaryLimits
from trading.common.operator_identity import LivePermission, OperatorIdentity
from trading.common.order_intent import OrderIntent, ProductType
from trading.common.reconciliation import ReadOnlyBrokerView
from trading.common.risk_manager import RiskManager
from trading.common.trading_account import TradingAccount

try:
    from trading.validation.angel_readonly import _is_expiry_still_tradable
except Exception:  # noqa: BLE001 -- optional; only used when an expiry_date is actually supplied
    _is_expiry_still_tradable = None  # type: ignore[assignment]

# Phase 15D.7 audit events. LIVE_AUTHORIZATION_CREATED/_CONSUMED already
# exist (EVENT_AUTHORIZATION_CREATED/_CONSUMED in live_authorization.py,
# Phase 15D.5) and are reused as-is -- not duplicated here.
EVENT_OPERATOR_AUTHENTICATED = "OPERATOR_AUTHENTICATED"
EVENT_OPERATOR_AUTHORIZATION_REQUESTED = "OPERATOR_AUTHORIZATION_REQUESTED"
EVENT_OPERATOR_AUTHORIZATION_DENIED = "OPERATOR_AUTHORIZATION_DENIED"
EVENT_OPERATOR_CONFIRMATION_ACCEPTED = "OPERATOR_CONFIRMATION_ACCEPTED"
EVENT_OPERATOR_CONFIRMATION_DECLINED = "OPERATOR_CONFIRMATION_DECLINED"


class WorkflowError(RuntimeError):
    """Raised at any workflow stage (request validation, preflight,
    confirmation) strictly BEFORE a `LiveAuthorization` is ever consumed.
    Distinct from `LiveAuthorizationError`, which is raised by the
    underlying store itself (Phase 15D.5, unchanged)."""


def validate_authorized_by(authorized_by: str) -> None:
    """Phase 15D.6 (Step 12): a defense-in-depth check retained unchanged
    -- non-empty, not secret-shaped. Phase 15D.7 no longer accepts this
    value directly from a caller; it is always derived from
    `OperatorIdentity.display_name` (see `AuthorizationRequest.authorized_by`
    below), but the same check still applies to whatever that name is."""
    if not authorized_by or not authorized_by.strip():
        raise WorkflowError("authorized_by must be a non-empty operator identifier")
    if redact_string(authorized_by) != authorized_by:
        raise WorkflowError("authorized_by must not contain secret-shaped content")


def _audit_operator_event(
    audit_trail: Any | None, event_type: str, operator: OperatorIdentity, *,
    permission: LivePermission | None = None, account_id: str = "", idempotency_key: str = "",
    correlation_id: str = "", reason: str = "",
) -> None:
    """Never raises -- an audit failure must not block or crash a gate
    (mirrors live_authorization.py's own `_audit()` and
    trading/api/security/audit.py's own "audit failures never break the
    request" precedent)."""
    if audit_trail is None:
        return
    try:
        audit_trail.append(
            event_type, correlation_id=correlation_id, account_id=account_id,
            idempotency_key=idempotency_key, operator_id=operator.operator_id,
            operator_display_name=operator.display_name, role=operator.role.value,
            authentication_source=operator.authentication_source,
            permission=(permission.value if permission is not None else ""), reason=reason,
        )
    except Exception:  # noqa: BLE001
        pass


@dataclass(frozen=True)
class AuthorizationRequest:
    account_id: str
    broker_id: str
    credential_reference: str
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
    operator: OperatorIdentity  # Phase 15D.7 -- replaces the old free-text `authorized_by` input
    lot_size: int | None = None
    expiry: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    @property
    def authorized_by(self) -> str:
        """Derived from the authenticated operator, never independently
        settable -- a caller can no longer just type a name."""
        return self.operator.display_name

    def __post_init__(self) -> None:
        # No implicit default account/broker/credential -- every field is
        # required as given, never inferred or substituted.
        if not self.account_id:
            raise WorkflowError("account_id is required -- no implicit default account")
        if not self.broker_id:
            raise WorkflowError("broker_id is required -- no implicit default broker")
        if not self.credential_reference:
            raise WorkflowError("credential_reference is required -- no credential fallback")
        if self.operator is None:
            raise WorkflowError("operator identity is required -- free-text authorized_by is no longer accepted")
        validate_authorized_by(self.authorized_by)


def validate_request(
    request: AuthorizationRequest, *, broker_manager: BrokerManager, idempotency_store: IdempotencyStore,
    risk_manager: RiskManager, strategy_id: str, authorization_service: AuthorizationService,
    audit_trail: Any | None = None, context: Any = None,
) -> None:
    """STEP 4 -- structural, connection-free validation. Fails closed
    (raises WorkflowError) on any problem; never mutates anything. Reuses
    RiskManager.validate() for risk rules rather than duplicating them.

    Phase 15D.7: the very first check, before any structural/risk check
    below, is that `request.operator` is authenticated, active, holds
    `REQUEST_LIVE_AUTHORIZATION`, and (if the resolved account declares an
    owner) owns this account -- see AuthorizationService.evaluate()."""
    try:
        account = broker_manager.get_account(request.account_id)
    except UnknownAccountError as exc:
        raise WorkflowError(f"unknown account_id {request.account_id!r}") from exc

    if request.operator.is_authenticated:
        _audit_operator_event(
            audit_trail, EVENT_OPERATOR_AUTHENTICATED, request.operator, account_id=request.account_id,
            idempotency_key=request.idempotency_key,
        )
    decision = authorization_service.evaluate(
        request.operator, LivePermission.REQUEST_LIVE_AUTHORIZATION, account=account,
    )
    _audit_operator_event(
        audit_trail,
        EVENT_OPERATOR_AUTHORIZATION_REQUESTED if decision.allowed else EVENT_OPERATOR_AUTHORIZATION_DENIED,
        request.operator, permission=LivePermission.REQUEST_LIVE_AUTHORIZATION, account_id=request.account_id,
        idempotency_key=request.idempotency_key, reason=decision.reason,
    )
    if not decision.allowed:
        raise WorkflowError(f"operator authorization denied: {decision.reason}")

    if account.broker_id != request.broker_id:
        raise WorkflowError(
            f"broker mismatch: account {request.account_id!r} is configured for "
            f"{account.broker_id!r}, request specified {request.broker_id!r}"
        )
    if account.credential_reference != request.credential_reference:
        raise WorkflowError(
            f"credential_reference mismatch: account {request.account_id!r} is configured for "
            f"{account.credential_reference!r}, request specified {request.credential_reference!r} "
            "-- no credential substitution is permitted"
        )

    if request.quantity <= 0:
        raise WorkflowError("quantity must be positive")
    if request.lot_size is not None and request.lot_size > 0 and request.quantity % request.lot_size != 0:
        raise WorkflowError(f"quantity {request.quantity} is not a multiple of lot_size {request.lot_size}")
    if request.max_order_value <= 0:
        raise WorkflowError("max_order_value must be positive")
    if request.daily_loss_limit <= 0 or request.strategy_loss_limit <= 0:
        raise WorkflowError("daily_loss_limit and strategy_loss_limit must be positive")
    if request.max_orders_per_day <= 0:
        raise WorkflowError("max_orders_per_day must be positive")

    if not request.idempotency_key:
        raise WorkflowError("idempotency_key is required")
    existing = idempotency_store.get(request.idempotency_key)
    if existing is not None:
        # Covers reused / historical / completed / pending / ambiguous keys
        # uniformly -- ANY existing record for this key means it is not
        # fresh, regardless of that record's own status.
        raise WorkflowError(
            f"idempotency_key {request.idempotency_key!r} already has a record "
            f"(status={existing.status}) -- a live authorization requires a fresh, novel key"
        )

    intent = OrderIntent(
        strategy_id=strategy_id, account_id=request.account_id, symbol=request.symbol, exchange="NFO",
        side=OrderSide(request.side), quantity=request.quantity, order_type=OrderType(request.order_type),
        product_type=ProductType(request.product_type), idempotency_key=request.idempotency_key,
    )
    # dry_run=True: this is a PREVIEW, not the real approval -- the one
    # real (mutating) validate() call happens inside execute() itself,
    # immediately before the broker call. A non-dry-run call here would
    # permanently mark this idempotency_key "seen" and consume a slot of
    # today's order-count budget for an order that may never be placed.
    risk_result = risk_manager.validate(intent, context, dry_run=True)
    if not risk_result.allowed:
        raise WorkflowError(f"RiskManager rejected this request: {risk_result.reason}")


@dataclass(frozen=True)
class PreflightResult:
    account_id: str
    broker_id: str
    credential_reference_masked: str
    symbol: str
    expiry: str
    quantity: int
    option_ltp: float | None
    estimated_value: float | None
    available_funds: float | None
    risk_passed: bool
    risk_reason: str
    canary_passed: bool
    canary_reason: str
    kill_switch_engaged: bool
    idempotency_novel: bool
    expiry_tradable: bool | None  # None = not checked (no expiry_date supplied)
    passed: bool
    failure_reason: str


def run_preflight(
    request: AuthorizationRequest, *, broker: BrokerClient, risk_manager: RiskManager,
    idempotency_store: IdempotencyStore, strategy_id: str, canary_limits: CanaryLimits | None = None,
    central_kill_switch: Any | None = None, expiry_date: date | None = None, now: Any = None,
    context: Any = None,
) -> PreflightResult:
    """STEP 5 -- entirely read-only. Uses `ReadOnlyBrokerView` (the same
    structurally mutation-incapable wrapper Phase 15D-RECON already
    built) so this function cannot place an order even by accident.
    Fails closed on any missing or ambiguous data -- never guesses, and
    never treats a missing bid/ask/OI as a liquidity confirmation (this
    project's adapters have never exposed that data; this function does
    not pretend otherwise)."""
    view = ReadOnlyBrokerView(broker)
    failure_reasons: list[str] = []

    option_ltp: float | None = None
    try:
        option_ltp = view.get_quote(request.symbol).last_price
    except Exception as exc:  # noqa: BLE001 -- fail closed, never guess a price
        failure_reasons.append(f"could not fetch option quote: {exc}")

    estimated_value = round(option_ltp * request.quantity, 2) if option_ltp is not None else None
    if estimated_value is not None and estimated_value > request.max_order_value:
        failure_reasons.append(
            f"estimated value {estimated_value} exceeds authorized max_order_value {request.max_order_value}"
        )

    available_funds: float | None = None
    get_funds = getattr(broker, "get_funds", None)
    if get_funds is not None:
        try:
            available_funds = get_funds().available_cash
        except Exception as exc:  # noqa: BLE001
            failure_reasons.append(f"could not fetch funds: {exc}")
        else:
            if estimated_value is not None and available_funds < estimated_value:
                failure_reasons.append(f"available_funds {available_funds} insufficient for estimated_value {estimated_value}")
    # else: adapter has no get_funds -- fails closed by leaving available_funds=None,
    # which by itself does not block (some adapters/tests legitimately lack it),
    # but the caller can see funds were never confirmed.

    expiry_tradable: bool | None = None
    if expiry_date is not None and _is_expiry_still_tradable is not None:
        from trading.market_data.market_hours import market_tz, now_in_tz

        check_now = now or now_in_tz(market_tz())
        expiry_tradable = _is_expiry_still_tradable(expiry_date, check_now)
        if not expiry_tradable:
            failure_reasons.append(f"expiry {expiry_date} is no longer tradable")

    intent = OrderIntent(
        strategy_id=strategy_id, account_id=request.account_id, symbol=request.symbol, exchange="NFO",
        side=OrderSide(request.side), quantity=request.quantity, order_type=OrderType(request.order_type),
        product_type=ProductType(request.product_type), idempotency_key=request.idempotency_key,
    )
    # dry_run=True -- see the matching comment in validate_request(). This
    # is still a PREVIEW; the real validate() call happens inside
    # execute().
    risk_result = risk_manager.validate(intent, context, dry_run=True)
    if not risk_result.allowed:
        failure_reasons.append(f"RiskManager: {risk_result.reason}")

    canary_passed, canary_reason = True, ""
    if canary_limits is not None:
        if request.account_id != canary_limits.account_id:
            canary_passed, canary_reason = False, "not the dedicated canary account"
        elif request.quantity > canary_limits.max_order_quantity:
            canary_passed, canary_reason = False, f"quantity {request.quantity} exceeds canary limit {canary_limits.max_order_quantity}"
        elif estimated_value is not None and estimated_value > canary_limits.max_order_value:
            canary_passed, canary_reason = False, f"estimated value {estimated_value} exceeds canary limit {canary_limits.max_order_value}"
        if not canary_passed:
            failure_reasons.append(f"CanaryLimits: {canary_reason}")

    kill_switch_engaged = bool(central_kill_switch is not None and central_kill_switch.engaged)
    if kill_switch_engaged:
        failure_reasons.append("central kill switch is engaged")

    existing_idem = idempotency_store.get(request.idempotency_key)
    idempotency_novel = existing_idem is None
    if not idempotency_novel:
        failure_reasons.append(f"idempotency_key is not novel (status={existing_idem.status})")

    passed = len(failure_reasons) == 0
    return PreflightResult(
        account_id=request.account_id, broker_id=request.broker_id,
        credential_reference_masked=mask_credential_reference(request.credential_reference),
        symbol=request.symbol, expiry=request.expiry, quantity=request.quantity, option_ltp=option_ltp,
        estimated_value=estimated_value, available_funds=available_funds, risk_passed=risk_result.allowed,
        risk_reason=risk_result.reason, canary_passed=canary_passed, canary_reason=canary_reason,
        kill_switch_engaged=kill_switch_engaged, idempotency_novel=idempotency_novel,
        expiry_tradable=expiry_tradable, passed=passed, failure_reason="; ".join(failure_reasons),
    )


ConfirmationProvider = Callable[[PreflightResult], bool]


def request_human_confirmation(preflight: PreflightResult, confirmation_provider: ConfirmationProvider) -> bool:
    """Only a return value of exactly `True` counts as approval. Missing
    input, a timeout represented as None, invalid input, arbitrary text,
    a raised exception, or reusing a previous confirmation's return value
    are all treated identically as NOT approved."""
    try:
        result = confirmation_provider(preflight)
    except Exception:  # noqa: BLE001 -- a broken confirmation provider must never be treated as approval
        return False
    return result is True


class LiveAuthorizationWorkflow:
    """Orchestrates REQUEST -> VALIDATE -> PREFLIGHT -> CONFIRM -> AUTHORIZE.
    EXECUTE is deliberately NOT a method on this class -- the caller
    passes `intent.metadata["authorization_id"]` to the EXISTING
    `StrategyExecutionEngine.execute()` unchanged (Phase 15D.5's own
    integration point), so there is exactly one execution path in this
    codebase, not two."""

    def __init__(self, live_authorization_store: SqliteLiveAuthorizationStore) -> None:
        self._store = live_authorization_store

    def request(self, request: AuthorizationRequest) -> LiveAuthorization:
        """Creates the authorization in PENDING. Callers must call
        `validate_request()` themselves first (kept as a free function,
        not a method, so it can be run without needing a store at all --
        e.g. as a pure pre-check).

        Phase 15D.7: `operator_id`/`operator_display_name`/
        `authentication_source`/`role` are stamped from
        `request.operator` -- these are the fields `try_consume()` (via
        execution.py) and a future confirm()-stage caller compare against,
        so a different operator can never replay or consume this
        authorization (see live_authorization.py's own updated
        `try_consume()`)."""
        return self._store.create(
            account_id=request.account_id, broker_id=request.broker_id,
            credential_reference=request.credential_reference, symbol=request.symbol, side=request.side,
            quantity=request.quantity, order_type=request.order_type, product_type=request.product_type,
            max_order_value=request.max_order_value, daily_loss_limit=request.daily_loss_limit,
            strategy_loss_limit=request.strategy_loss_limit, max_orders_per_day=request.max_orders_per_day,
            idempotency_key=request.idempotency_key, authorized_by=request.authorized_by,
            ttl_seconds=request.ttl_seconds,
            operator_id=request.operator.operator_id, operator_display_name=request.operator.display_name,
            authentication_source=request.operator.authentication_source, role=request.operator.role.value,
            permission_used=LivePermission.REQUEST_LIVE_AUTHORIZATION.value,
        )

    def confirm(
        self, authorization_id: str, preflight: PreflightResult, confirmation_provider: ConfirmationProvider,
        *, operator: OperatorIdentity, account: TradingAccount, authorization_service: AuthorizationService,
        audit_trail: Any | None = None,
    ) -> LiveAuthorization:
        """STEP 6/7/8: a failed preflight, a denied operator, or a
        missing/invalid/declined human confirmation all REVOKE the pending
        authorization (never leave it dangling in PENDING) and raise --
        the authorization only ever reaches AUTHORIZED (via the underlying
        store's own `validate()`) immediately after an explicit `True`
        confirmation, from an operator holding `CONFIRM_LIVE_ACTION` on
        this account, against a PASSED preflight.

        `operator` here is the CONFIRMING identity -- it may be the same
        person who called `request()`, or a different one (e.g. a second
        approver); both are durably recorded (the requester on the
        authorization row itself via `request()`'s stamped operator
        fields, the confirmer via the OPERATOR_CONFIRMATION_ACCEPTED/
        _DECLINED audit event emitted here)."""
        if not preflight.passed:
            self._store.revoke(authorization_id, reason=f"preflight failed: {preflight.failure_reason}")
            raise WorkflowError(f"preflight failed -- authorization revoked: {preflight.failure_reason}")

        decision = authorization_service.evaluate(operator, LivePermission.CONFIRM_LIVE_ACTION, account=account)
        if not decision.allowed:
            self._store.revoke(authorization_id, reason=f"operator not authorized to confirm: {decision.reason}")
            _audit_operator_event(
                audit_trail, EVENT_OPERATOR_AUTHORIZATION_DENIED, operator,
                permission=LivePermission.CONFIRM_LIVE_ACTION, account_id=account.account_id, reason=decision.reason,
            )
            raise WorkflowError(f"operator not authorized to confirm this action: {decision.reason}")

        approved = request_human_confirmation(preflight, confirmation_provider)
        if not approved:
            self._store.revoke(authorization_id, reason="human confirmation was not given")
            _audit_operator_event(
                audit_trail, EVENT_OPERATOR_CONFIRMATION_DECLINED, operator,
                permission=LivePermission.CONFIRM_LIVE_ACTION, account_id=account.account_id,
                reason="missing, invalid, or declined",
            )
            raise WorkflowError("human confirmation was not given (missing, invalid, or declined) -- authorization revoked")

        _audit_operator_event(
            audit_trail, EVENT_OPERATOR_CONFIRMATION_ACCEPTED, operator,
            permission=LivePermission.CONFIRM_LIVE_ACTION, account_id=account.account_id,
        )
        try:
            return self._store.validate(authorization_id)
        except LiveAuthorizationError as exc:
            # The authorization itself is no longer PENDING (already
            # EXPIRED, REVOKED, or otherwise not confirmable) -- surface
            # this the same way every other confirm()-stage failure is
            # surfaced, as a WorkflowError, rather than leaking the
            # underlying store's own exception type to callers of this
            # workflow layer.
            raise WorkflowError(f"authorization could not be validated: {exc}") from exc
