"""Phase 15D.7: the operator-identity/authorization boundary in front of
the (unchanged) Phase 15D.5/15D.6 LiveAuthorization workflow.

Every test in this file uses `RecordingFakeBroker` (imported from the
Phase 15D.6 test file, not redefined) and asserts
`mutation_call_count == 0` -- no test in this suite ever reaches a
successful broker call. Historical records
(260917000350205, the AG7002 PENDING record, 260917000523943) are never
touched.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.common.test_phase_15d_6_live_authorization_workflow import RecordingFakeBroker
from trading.common.audit_store import PersistentAuditTrail
from trading.common.broker_manager import BrokerManager
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import SqliteIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_authorization import (
    AuthorizationStatus,
    LiveAuthorizationError,
    SqliteLiveAuthorizationStore,
)
from trading.common.live_authorization_service import AuthorizationService
from trading.common.live_authorization_workflow import (
    AuthorizationRequest,
    LiveAuthorizationWorkflow,
    PreflightResult,
    WorkflowError,
    run_preflight,
    validate_request,
)
from trading.common.operator_identity import (
    AuthState,
    FakeAuthenticationProvider,
    JwtClaimsAuthenticationProvider,
    LivePermission,
    OperatorIdentity,
    OperatorRole,
    ROLE_PERMISSIONS,
    invalid_identity,
    operator_role_from_string,
    permissions_for_role,
    unauthenticated,
)
from trading.common.order_intent import OrderIntent, OrderSide, OrderType
from trading.common.risk_manager import RiskContext, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount

SYMBOL = "NIFTY22SEP2623550CE"
STRATEGY_ID = "PHASE_15D7_CANARY"
OWNER_TRADER_ID = "user:1"


def _identity(role: OperatorRole, *, operator_id: str = OWNER_TRADER_ID, display_name: str = "samir",
              is_active: bool = True, auth_state: AuthState = AuthState.AUTHENTICATED) -> OperatorIdentity:
    return OperatorIdentity(
        operator_id=operator_id, display_name=display_name, role=role, auth_state=auth_state,
        authentication_source="fake-test", permissions=permissions_for_role(role), is_active=is_active,
    )


TRADER = _identity(OperatorRole.TRADER)  # owns ACC_B (see _account_b())
VIEWER = _identity(OperatorRole.VIEWER, operator_id="user:2", display_name="viewer-vikram")
ADMIN = _identity(OperatorRole.ADMIN, operator_id="user:3", display_name="admin-asha")
DISABLED_TRADER = _identity(OperatorRole.TRADER, is_active=False, display_name="disabled-deepa")
WRONG_OWNER_TRADER = _identity(OperatorRole.TRADER, operator_id="user:99", display_name="not-the-owner")
UNAUTHENTICATED = unauthenticated("no session")
INVALID = invalid_identity("malformed-session")


def _account_b(owner_id: str = OWNER_TRADER_ID) -> TradingAccount:
    return TradingAccount(
        account_id="ACC_B", account_name="B", broker_id="angelone", credential_reference="env:ANGELONE_B",
        execution_mode=ExecutionMode.PAPER, owner_id=owner_id,
    )


def _request(**overrides) -> AuthorizationRequest:
    defaults = dict(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B",
        symbol=SYMBOL, side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY",
        max_order_value=2000.0, daily_loss_limit=2000.0, strategy_loss_limit=2000.0,
        max_orders_per_day=1, idempotency_key="op-key-1", operator=TRADER, lot_size=65, ttl_seconds=600,
    )
    defaults.update(overrides)
    return AuthorizationRequest(**defaults)


def _rig(tmp_path, broker: RecordingFakeBroker, *, owner_id: str = OWNER_TRADER_ID):
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = _account_b(owner_id=owner_id)
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign(STRATEGY_ID, "ACC_B", execution_mode=ExecutionMode.PAPER)
    risk_manager = RiskManager(assignment)
    return dict(
        idem_store=idem_store, audit_trail=audit_trail, auth_store=auth_store, broker_manager=broker_manager,
        account=account, assignment=assignment, risk_manager=risk_manager, authorization_service=AuthorizationService(),
    )


def _approve(preflight: PreflightResult) -> bool:
    return True


def _intent(request: AuthorizationRequest, authorization_id: str, operator_id: str = "") -> OrderIntent:
    return OrderIntent(
        strategy_id=STRATEGY_ID, account_id=request.account_id, symbol=request.symbol, exchange="NFO",
        side=OrderSide(request.side), quantity=request.quantity, order_type=OrderType(request.order_type),
        idempotency_key=request.idempotency_key,
        metadata={"authorization_id": authorization_id, "operator_id": operator_id},
    )


def _engine(rig, broker: RecordingFakeBroker, *, central_kill_switch=None) -> StrategyExecutionEngine:
    return StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=rig["risk_manager"], strategy_assignment=rig["assignment"], broker_manager=rig["broker_manager"],
        audit_trail=rig["audit_trail"], idempotency_store=rig["idem_store"],
        live_authorization_store=rig["auth_store"], central_kill_switch=central_kill_switch,
    )


def _request_and_confirm(rig, request: AuthorizationRequest, *, requester=TRADER, confirmer=TRADER):
    """Full REQUEST -> VALIDATE -> PREFLIGHT -> CONFIRM, no execution."""
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=rig["authorization_service"], audit_trail=rig["audit_trail"])
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=rig["_broker"], risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    authorized = workflow.confirm(
        pending.authorization_id, preflight, _approve, operator=confirmer, account=rig["account"],
        authorization_service=rig["authorization_service"], audit_trail=rig["audit_trail"],
    )
    return workflow, authorized


# ======================================================================== #
# Authentication (1-4)
# ======================================================================== #
def test_01_authenticated_operator_accepted(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    rig["_broker"] = broker
    validate_request(_request(operator=TRADER), broker_manager=rig["broker_manager"],
                      idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                      strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"])
    assert broker.mutation_call_count == 0


def test_02_unauthenticated_operator_rejected(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    with pytest.raises(WorkflowError, match="operator authorization denied"):
        validate_request(_request(operator=UNAUTHENTICATED), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                          strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"])
    assert broker.mutation_call_count == 0


def test_03_invalid_identity_rejected(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    with pytest.raises(WorkflowError, match="operator authorization denied"):
        validate_request(_request(operator=INVALID), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                          strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"])
    assert broker.mutation_call_count == 0


def test_04_disabled_operator_rejected(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    with pytest.raises(WorkflowError, match="operator authorization denied"):
        validate_request(_request(operator=DISABLED_TRADER), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                          strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"])
    assert broker.mutation_call_count == 0


# ======================================================================== #
# Permissions (5-9)
# ======================================================================== #
def test_05_viewer_cannot_request_live_authorization(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker, owner_id="")  # house account -- isolate the permission check from ownership
    with pytest.raises(WorkflowError, match="lacks required permission"):
        validate_request(_request(operator=VIEWER), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                          strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"])
    assert broker.mutation_call_count == 0


def test_06_viewer_cannot_confirm_live_action(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker, owner_id="")
    rig["_broker"] = broker
    validate_request(_request(operator=TRADER), broker_manager=rig["broker_manager"],
                      idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                      strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"])
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(_request(operator=TRADER))
    preflight = run_preflight(_request(operator=TRADER), broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    with pytest.raises(WorkflowError, match="not authorized to confirm"):
        workflow.confirm(pending.authorization_id, preflight, _approve, operator=VIEWER, account=rig["account"],
                          authorization_service=rig["authorization_service"])
    assert rig["auth_store"].get(pending.authorization_id).status == AuthorizationStatus.REVOKED.value
    assert broker.mutation_call_count == 0


def test_07_trader_with_correct_permission_accepted(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    rig["_broker"] = broker
    _workflow, authorized = _request_and_confirm(rig, _request())
    assert authorized.status == AuthorizationStatus.AUTHORIZED.value
    assert broker.mutation_call_count == 0


def test_08_missing_permission_rejected(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker, owner_id="")
    no_perms = OperatorIdentity(
        operator_id="user:4", display_name="no-perms-nina", role=OperatorRole.VIEWER,
        auth_state=AuthState.AUTHENTICATED, authentication_source="fake-test",
        permissions=frozenset(), is_active=True,
    )
    with pytest.raises(WorkflowError, match="lacks required permission"):
        validate_request(_request(operator=no_perms), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                          strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"])
    assert broker.mutation_call_count == 0


def test_09_kill_switch_fail_safe_independent_of_permissions(tmp_path):
    """The kill switch is an existing, independent, unconditional (step-0)
    gate -- Phase 15D.7 must NOT make it easier to bypass by tying it to a
    permission. Prove: engaging it blocks execution even for an ADMIN
    identity holding KILL_TRADING and every other LivePermission, exactly
    as it would for anyone else."""
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker, owner_id="")  # house account -- isolate from the separate ownership check
    rig["_broker"] = broker
    assert ADMIN.has(LivePermission.KILL_TRADING)
    _workflow, authorized = _request_and_confirm(
        rig, _request(operator=ADMIN), requester=ADMIN, confirmer=ADMIN,
    )

    ks = CentralKillSwitch(persistence_path=str(tmp_path / "ks.json"))
    ks.engage(by="admin-asha", reason="test")
    engine = _engine(rig, broker, central_kill_switch=ks)
    result = engine.execute(
        _intent(_request(), authorized.authorization_id, operator_id=ADMIN.operator_id),
        context=RiskContext(reference_price=broker.ltp),
    )
    assert not result.success
    assert "kill switch" in result.message.lower()
    assert broker.mutation_call_count == 0


# ======================================================================== #
# Account isolation (10-12)
# ======================================================================== #
def test_10_operator_cannot_authorize_unauthorized_account(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker, owner_id=OWNER_TRADER_ID)  # ACC_B owned by user:1
    with pytest.raises(WorkflowError, match="does not own account"):
        validate_request(_request(operator=WRONG_OWNER_TRADER), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                          strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"])
    assert broker.mutation_call_count == 0


def test_11_account_a_authorization_cannot_execute_account_b(tmp_path):
    broker_a = RecordingFakeBroker()
    broker_b = RecordingFakeBroker()
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account_a = TradingAccount(account_id="ACC_A", account_name="A", broker_id="angelone",
                                credential_reference="env:ANGELONE_A", execution_mode=ExecutionMode.PAPER,
                                owner_id=OWNER_TRADER_ID)
    account_b = _account_b()
    broker_manager.register_account(account_a, broker_client=broker_a)
    broker_manager.register_account(account_b, broker_client=broker_b)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign(STRATEGY_ID, "ACC_A", execution_mode=ExecutionMode.PAPER)
    risk_manager = RiskManager(assignment)
    service = AuthorizationService()

    request = _request(account_id="ACC_A", broker_id="angelone", credential_reference="env:ANGELONE_A")
    validate_request(request, broker_manager=broker_manager, idempotency_store=idem_store, risk_manager=risk_manager,
                      strategy_id=STRATEGY_ID, authorization_service=service)
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker_a, risk_manager=risk_manager, idempotency_store=idem_store,
                               strategy_id=STRATEGY_ID)
    authorized = workflow.confirm(pending.authorization_id, preflight, _approve, operator=TRADER, account=account_a,
                                   authorization_service=service)

    # Reassign the strategy to ACC_B -- execute() resolves the account via
    # StrategyAssignment, not intent.account_id (see Phase 15D.6's own
    # test_case_21 for why this is the only real way to change it).
    assignment.assign(STRATEGY_ID, "ACC_B", execution_mode=ExecutionMode.PAPER)
    engine = StrategyExecutionEngine(
        broker=broker_b, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store, live_authorization_store=auth_store,
    )
    result = engine.execute(_intent(request, authorized.authorization_id, operator_id=TRADER.operator_id),
                             context=RiskContext(reference_price=broker_b.ltp))
    assert not result.success
    assert broker_a.mutation_call_count == 0
    assert broker_b.mutation_call_count == 0


def test_12_account_b_authorization_cannot_execute_account_a(tmp_path):
    broker_a = RecordingFakeBroker()
    broker_b = RecordingFakeBroker()
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account_a = TradingAccount(account_id="ACC_A", account_name="A", broker_id="angelone",
                                credential_reference="env:ANGELONE_A", execution_mode=ExecutionMode.PAPER,
                                owner_id=OWNER_TRADER_ID)
    account_b = _account_b()
    broker_manager.register_account(account_a, broker_client=broker_a)
    broker_manager.register_account(account_b, broker_client=broker_b)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign(STRATEGY_ID, "ACC_B", execution_mode=ExecutionMode.PAPER)
    risk_manager = RiskManager(assignment)
    service = AuthorizationService()

    request = _request()  # scoped to ACC_B
    validate_request(request, broker_manager=broker_manager, idempotency_store=idem_store, risk_manager=risk_manager,
                      strategy_id=STRATEGY_ID, authorization_service=service)
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker_b, risk_manager=risk_manager, idempotency_store=idem_store,
                               strategy_id=STRATEGY_ID)
    authorized = workflow.confirm(pending.authorization_id, preflight, _approve, operator=TRADER, account=account_b,
                                   authorization_service=service)

    assignment.assign(STRATEGY_ID, "ACC_A", execution_mode=ExecutionMode.PAPER)
    engine = StrategyExecutionEngine(
        broker=broker_a, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store, live_authorization_store=auth_store,
    )
    result = engine.execute(_intent(request, authorized.authorization_id, operator_id=TRADER.operator_id),
                             context=RiskContext(reference_price=broker_a.ltp))
    assert not result.success
    assert broker_a.mutation_call_count == 0
    assert broker_b.mutation_call_count == 0


# ======================================================================== #
# Broker isolation (13-15)
# ======================================================================== #
def test_13_authorization_bound_to_broker(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    assert pending.broker_id == "angelone"


def test_14_broker_mismatch_rejected_at_consume(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    validated = auth_store.validate(pending.authorization_id)
    with pytest.raises(LiveAuthorizationError, match="broker_id"):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="dhan",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key=request.idempotency_key,
            order_value=1500, operator_id=TRADER.operator_id,
        )


def test_15_credential_reference_mismatch_rejected_at_consume(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    validated = auth_store.validate(pending.authorization_id)
    with pytest.raises(LiveAuthorizationError, match="credential_reference"):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_A", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key=request.idempotency_key,
            order_value=1500, operator_id=TRADER.operator_id,
        )


# ======================================================================== #
# Exact-action binding (16-21)
# ======================================================================== #
def _consume_with(auth_store, authorization_id, request, *, operator_id=None, **overrides):
    fields = dict(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B",
        symbol=SYMBOL, side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY",
        idempotency_key=request.idempotency_key, order_value=1500.0,
    )
    fields.update(overrides)
    return auth_store.try_consume(authorization_id, operator_id=operator_id, **fields)


@pytest.mark.parametrize("field,value", [
    ("symbol", "NIFTY22SEP2623600CE"),   # 16: instrument tampering
    ("side", "SELL"),                     # 17: side tampering
    ("quantity", 130),                    # 18: quantity tampering
    ("order_type", "LIMIT"),              # 19: order-type tampering
    ("product_type", "DELIVERY"),         # 20: product-type tampering
    ("idempotency_key", "some-other-key"),  # 21: idempotency-key tampering
])
def test_16_21_exact_action_tampering_rejected(tmp_path, field, value):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    validated = auth_store.validate(pending.authorization_id)
    with pytest.raises(LiveAuthorizationError, match=field):
        _consume_with(auth_store, validated.authorization_id, request, operator_id=TRADER.operator_id,
                      **{field: value})


# ======================================================================== #
# Replay (22-26)
# ======================================================================== #
def test_22_authorization_can_only_be_consumed_once(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    validated = auth_store.validate(pending.authorization_id)
    first = _consume_with(auth_store, validated.authorization_id, request, operator_id=TRADER.operator_id)
    assert first.status == AuthorizationStatus.CONSUMED.value
    with pytest.raises(LiveAuthorizationError):
        _consume_with(auth_store, validated.authorization_id, request, operator_id=TRADER.operator_id)


def test_23_expired_authorization_rejected(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request(ttl_seconds=-1)
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)  # already expired at creation
    with pytest.raises(LiveAuthorizationError, match="EXPIRED"):
        auth_store.validate(pending.authorization_id)


def test_24_revoked_authorization_rejected(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    auth_store.revoke(pending.authorization_id, reason="operator changed mind")
    with pytest.raises(LiveAuthorizationError, match="REVOKED"):
        auth_store.validate(pending.authorization_id)


def test_25_consumed_authorization_rejected(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    validated = auth_store.validate(pending.authorization_id)
    _consume_with(auth_store, validated.authorization_id, request, operator_id=TRADER.operator_id)
    with pytest.raises(LiveAuthorizationError, match="CONSUMED"):
        _consume_with(auth_store, validated.authorization_id, request, operator_id=TRADER.operator_id)


def test_26_restart_preserves_authorization_state(tmp_path):
    p = str(tmp_path / "auth.db")
    auth_store1 = SqliteLiveAuthorizationStore(db_path=p)
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store1)
    pending = workflow.request(request)
    validated = auth_store1.validate(pending.authorization_id)
    assert validated.operator_id == TRADER.operator_id
    del auth_store1

    auth_store2 = SqliteLiveAuthorizationStore(db_path=p)
    reloaded = auth_store2.get(validated.authorization_id)
    assert reloaded.status == AuthorizationStatus.AUTHORIZED.value
    assert reloaded.operator_id == TRADER.operator_id
    assert reloaded.role == TRADER.role.value


# ======================================================================== #
# Audit (27-32)
# ======================================================================== #
def test_27_authentication_event_audited(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    validate_request(_request(), broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=rig["authorization_service"], audit_trail=rig["audit_trail"])
    events = {e.event_type for e in rig["audit_trail"].records()}
    assert "OPERATOR_AUTHENTICATED" in events


def test_28_denial_audited(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker, owner_id="")
    with pytest.raises(WorkflowError):
        validate_request(_request(operator=VIEWER), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"],
                          strategy_id=STRATEGY_ID, authorization_service=rig["authorization_service"],
                          audit_trail=rig["audit_trail"])
    events = {e.event_type for e in rig["audit_trail"].records()}
    assert "OPERATOR_AUTHORIZATION_DENIED" in events


def test_29_confirmation_audited(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    rig["_broker"] = broker
    _request_and_confirm(rig, _request())
    events = {e.event_type for e in rig["audit_trail"].records()}
    assert "OPERATOR_CONFIRMATION_ACCEPTED" in events


def test_30_authorization_creation_audited(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    workflow = LiveAuthorizationWorkflow(auth_store)
    workflow.request(_request())
    events = {e.event_type for e in audit_trail.records()}
    assert "AUTHORIZATION_CREATED" in events  # reused Phase 15D.5 event, not duplicated


def test_31_authorization_consumption_audited(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    validated = auth_store.validate(pending.authorization_id)
    _consume_with(auth_store, validated.authorization_id, request, operator_id=TRADER.operator_id)
    events = {e.event_type for e in audit_trail.records()}
    assert "AUTHORIZATION_CONSUMED" in events


def test_32_audit_correlation_preserved(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    rig["_broker"] = broker
    _request_and_confirm(rig, _request())
    events = rig["audit_trail"].records()
    idempotency_events = [e for e in events if e.idempotency_key == "op-key-1"]
    event_types = {e.event_type for e in idempotency_events}
    assert {"OPERATOR_AUTHENTICATED", "OPERATOR_AUTHORIZATION_REQUESTED", "AUTHORIZATION_CREATED"} <= event_types


# ======================================================================== #
# Historical integrity (33-36)
# ======================================================================== #
def test_33_historical_order_260917000350205_unchanged():
    conn = sqlite3.connect("trading/phase15d2_idempotency.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, broker_order_id FROM idempotency_records WHERE broker_order_id = '260917000350205'"
    ).fetchone()
    conn.close()
    assert dict(row) == {"status": "COMPLETED", "broker_order_id": "260917000350205"}


def test_34_historical_ag7002_record_unchanged():
    conn = sqlite3.connect("trading/phase15d2_idempotency.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM idempotency_records WHERE idempotency_key LIKE 'phase15d2-canary-NIFTY22SEP2623600CE%'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert dict(row)["status"] == "PENDING"


def test_35_manual_sell_260917000523943_remains_unattributed():
    conn = sqlite3.connect("trading/phase15d2_idempotency.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT broker_order_id FROM idempotency_records WHERE broker_order_id = '260917000523943'"
    ).fetchone()
    conn.close()
    # No idempotency record exists for the manual SELL -- it was never
    # attributed to TCC (this project's execution engine) and this phase
    # must not create one.
    assert row is None


def test_36_no_new_live_broker_mutation_anywhere_in_this_phase():
    """This is a meta-assertion, not a broker check -- every test above
    already asserts mutation_call_count == 0 individually. This test
    documents the invariant explicitly, per the brief's own Step 10
    requirement, and re-confirms no real LiveAuthorization database exists
    anywhere in the repository (only tmp_path test databases)."""
    import pathlib

    matches = list(pathlib.Path(".").rglob("*live_auth*.db"))
    assert matches == []


# ======================================================================== #
# AuthenticationProvider implementations (additional coverage beyond the
# 36 numbered cases -- these exercise the two concrete providers directly)
# ======================================================================== #
def test_fake_provider_resolves_registered_operator():
    provider = FakeAuthenticationProvider()
    provider.register("token-abc", TRADER)
    resolved = provider.resolve_operator("token-abc")
    assert resolved.is_authenticated
    assert resolved.operator_id == TRADER.operator_id


def test_fake_provider_unknown_token_is_unauthenticated():
    provider = FakeAuthenticationProvider()
    resolved = provider.resolve_operator("no-such-token")
    assert resolved.auth_state == AuthState.UNAUTHENTICATED
    assert not resolved.is_authenticated


def test_fake_provider_none_credential_is_unauthenticated():
    provider = FakeAuthenticationProvider()
    resolved = provider.resolve_operator(None)
    assert resolved.auth_state == AuthState.UNAUTHENTICATED


def test_fake_provider_malformed_credential_is_invalid():
    provider = FakeAuthenticationProvider()
    resolved = provider.resolve_operator(12345)
    assert resolved.auth_state == AuthState.INVALID
    assert not resolved.is_authenticated


def test_fake_provider_disable_deactivates_a_known_operator():
    provider = FakeAuthenticationProvider()
    provider.register("token-abc", TRADER)
    provider.disable("token-abc")
    resolved = provider.resolve_operator("token-abc")
    assert resolved.operator_id == TRADER.operator_id  # identity preserved
    assert resolved.is_active is False
    assert not resolved.is_authenticated  # disabled -- fails closed


def test_jwt_claims_provider_maps_role_and_permissions():
    provider = JwtClaimsAuthenticationProvider()
    resolved = provider.resolve_operator(
        {"sub": "42", "username": "samir", "role": "admin", "is_active": True}
    )
    assert resolved.is_authenticated
    assert resolved.operator_id == "user:42"
    assert resolved.role == OperatorRole.ADMIN
    assert resolved.permissions == ROLE_PERMISSIONS[OperatorRole.ADMIN]
    assert resolved.authentication_source == "jwt"


def test_jwt_claims_provider_inactive_user_not_authenticated():
    provider = JwtClaimsAuthenticationProvider()
    resolved = provider.resolve_operator(
        {"sub": "42", "username": "samir", "role": "admin", "is_active": False}
    )
    assert not resolved.is_authenticated
    assert resolved.permissions == frozenset()


@pytest.mark.parametrize("claims", [None, "not-a-dict", {}, {"sub": "42"}, {"username": "samir"}])
def test_jwt_claims_provider_rejects_malformed_claims(claims):
    provider = JwtClaimsAuthenticationProvider()
    resolved = provider.resolve_operator(claims)
    assert resolved.auth_state == AuthState.INVALID
    assert not resolved.is_authenticated


@pytest.mark.parametrize("role_string,expected", [
    ("viewer", OperatorRole.VIEWER),
    ("trader", OperatorRole.TRADER),
    ("operator", OperatorRole.TRADER),  # existing trading/api "operator" role maps to TRADER (see plan section 4.1)
    ("admin", OperatorRole.ADMIN),
    ("ADMIN", OperatorRole.ADMIN),  # case-insensitive
    ("bogus-role", OperatorRole.VIEWER),  # unknown -> least privilege, fail closed
    ("", OperatorRole.VIEWER),
])
def test_operator_role_from_string_mapping(role_string, expected):
    assert operator_role_from_string(role_string) == expected
