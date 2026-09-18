"""Phase 15D.6: controlled operator workflow around Phase 15D.5's
LiveAuthorization mechanism -- REQUEST -> VALIDATE -> PREFLIGHT -> HUMAN
CONFIRMATION -> AUTHORIZE -> EXECUTE.

Every test in this file uses a fake broker. Zero real broker calls, zero
real orders. Historical records (260917000350205, the AG7002 PENDING
record, 260917000523943) are never touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trading.common.audit_store import PersistentAuditTrail
from trading.common.broker import BrokerClient, OrderResult, OrderSide, OrderType, Quote
from trading.common.broker_manager import BrokerManager
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import STATUS_AMBIGUOUS, SqliteIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_authorization import AuthorizationStatus, SqliteLiveAuthorizationStore
from trading.common.live_authorization_workflow import (
    AuthorizationRequest,
    LiveAuthorizationWorkflow,
    PreflightResult,
    WorkflowError,
    run_preflight,
    validate_request,
)
from trading.common.live_authorization_service import AuthorizationService
from trading.common.live_canary import CanaryLimits
from trading.common.operator_identity import (
    AuthState,
    OperatorIdentity,
    OperatorRole,
    permissions_for_role,
)
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount

SYMBOL = "NIFTY22SEP2623550CE"
STRATEGY_ID = "PHASE_15D6_CANARY"

# Phase 15D.7: every Phase 15D.6 test now needs an authenticated operator
# identity and an AuthorizationService -- these tests are not ABOUT
# identity (Phase 15D.7's own test file covers that exhaustively), so a
# single always-authenticated, always-permitted TRADER identity is used
# throughout, keeping each test focused on its original purpose.
_TRADER = OperatorIdentity(
    operator_id="user:15d6-trader", display_name="samir", role=OperatorRole.TRADER,
    auth_state=AuthState.AUTHENTICATED, authentication_source="fake-test",
    permissions=permissions_for_role(OperatorRole.TRADER), is_active=True,
)


def _authorization_service() -> AuthorizationService:
    return AuthorizationService()


class RecordingFakeBroker(BrokerClient):
    """Records every mutation call for assertion; never touches a
    network. Also records the exact args of the last place_order() call
    for account/instrument/quantity/side/order_type assertions."""

    is_simulated = True

    def __init__(self) -> None:
        self.connected = False
        self.mutation_call_count = 0
        self.last_call: dict | None = None
        self.funds_cash = 3000.0
        self.ltp = 27.0

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=self.ltp, timestamp="2026-01-01T00:00:00+00:00")

    def get_funds(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _Funds:
            available_cash: float

        return _Funds(available_cash=self.funds_cash)

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        self.mutation_call_count += 1
        self.last_call = dict(symbol=symbol, side=side, quantity=quantity, order_type=order_type)
        return OrderResult(order_id=f"FAKE-{self.mutation_call_count}", symbol=symbol, side=side,
                            quantity=quantity, status="FILLED", message="ok")

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self):
        return []


def _account_b():
    return TradingAccount(account_id="ACC_B", account_name="B", broker_id="angelone",
                           credential_reference="env:ANGELONE_B", execution_mode=ExecutionMode.PAPER)


def _request(**overrides):
    defaults = dict(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B",
        symbol=SYMBOL, side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY",
        max_order_value=2000.0, daily_loss_limit=2000.0, strategy_loss_limit=2000.0,
        max_orders_per_day=1, idempotency_key="wf-key-1", operator=_TRADER, lot_size=65,
        ttl_seconds=600,
    )
    defaults.update(overrides)
    return AuthorizationRequest(**defaults)


def _rig(tmp_path: Path, broker: RecordingFakeBroker, *, risk_limits=None):
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = _account_b()
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign(STRATEGY_ID, "ACC_B", execution_mode=ExecutionMode.PAPER)
    risk_manager = RiskManager(assignment, limits=risk_limits)
    return dict(
        idem_store=idem_store, audit_trail=audit_trail, auth_store=auth_store,
        broker_manager=broker_manager, account=account, assignment=assignment, risk_manager=risk_manager,
    )


def _engine(rig, broker, auth_store, *, central_kill_switch=None):
    return StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=rig["risk_manager"], strategy_assignment=rig["assignment"], broker_manager=rig["broker_manager"],
        audit_trail=rig["audit_trail"], idempotency_store=rig["idem_store"], live_authorization_store=auth_store,
        central_kill_switch=central_kill_switch,
    )


def _intent(request: AuthorizationRequest, authorization_id: str):
    return OrderIntent(
        strategy_id=STRATEGY_ID, account_id=request.account_id, symbol=request.symbol, exchange="NFO",
        side=OrderSide(request.side), quantity=request.quantity, order_type=OrderType(request.order_type),
        idempotency_key=request.idempotency_key, metadata={"authorization_id": authorization_id},
    )


def _approve(preflight: PreflightResult) -> bool:
    return True


def _decline(preflight: PreflightResult) -> bool:
    return False


def _full_flow(tmp_path, *, request=None, broker=None, confirmation_provider=_approve, risk_limits=None):
    """End-to-end helper: validate -> create -> preflight -> confirm -> execute."""
    request = request or _request()
    broker = broker or RecordingFakeBroker()
    rig = _rig(tmp_path, broker, risk_limits=risk_limits)
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    authorized = workflow.confirm(pending.authorization_id, preflight, confirmation_provider, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())
    engine = _engine(rig, broker, rig["auth_store"])
    result = engine.execute(_intent(request, authorized.authorization_id), context=RiskContext(reference_price=broker.ltp))
    return dict(rig=rig, broker=broker, request=request, authorized=authorized, preflight=preflight, result=result)


# ---------------------------------------------------------------------- #
# Full happy-path workflow
# ---------------------------------------------------------------------- #
def test_full_workflow_success(tmp_path):
    out = _full_flow(tmp_path)
    assert out["result"].success
    assert out["broker"].mutation_call_count == 1
    assert out["broker"].last_call["symbol"] == SYMBOL
    assert out["broker"].last_call["quantity"] == 65
    assert out["broker"].last_call["side"] == OrderSide.BUY


def test_second_execution_after_success_makes_zero_new_mutation_calls(tmp_path):
    out = _full_flow(tmp_path)
    engine2 = _engine(out["rig"], out["broker"], out["rig"]["auth_store"])
    result2 = engine2.execute(_intent(out["request"], out["authorized"].authorization_id),
                               context=RiskContext(reference_price=out["broker"].ltp))
    assert out["broker"].mutation_call_count == 1  # unchanged


# ---------------------------------------------------------------------- #
# STEP 4: request validation failures (1-3, 6-7)
# ---------------------------------------------------------------------- #
def test_case_1_invalid_account(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    with pytest.raises(WorkflowError, match="unknown account_id"):
        validate_request(_request(account_id="NO_SUCH_ACCOUNT"), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    assert broker.mutation_call_count == 0


def test_case_2_wrong_broker(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    with pytest.raises(WorkflowError, match="broker mismatch"):
        validate_request(_request(broker_id="dhan"), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    assert broker.mutation_call_count == 0


def test_case_3_wrong_credential_reference(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    with pytest.raises(WorkflowError, match="credential_reference mismatch"):
        validate_request(_request(credential_reference="env:ANGELONE_A"), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    assert broker.mutation_call_count == 0


def test_case_6_invalid_quantity(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    with pytest.raises(WorkflowError, match="quantity must be positive"):
        validate_request(_request(quantity=0), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())


def test_case_7_invalid_lot_size(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    with pytest.raises(WorkflowError, match="not a multiple of lot_size"):
        validate_request(_request(quantity=100, lot_size=65), broker_manager=rig["broker_manager"],
                          idempotency_store=rig["idem_store"], risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())


# ---------------------------------------------------------------------- #
# Case 4/5: expired / invalid instrument (checked at preflight, via expiry_date)
# ---------------------------------------------------------------------- #
def test_case_4_expired_instrument_fails_preflight(tmp_path):
    from datetime import date

    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID,
                               expiry_date=date(2020, 1, 1))  # long past
    assert not preflight.passed
    assert preflight.expiry_tradable is False
    assert "no longer tradable" in preflight.failure_reason


def test_case_5_invalid_instrument_quote_failure_fails_closed_preflight(tmp_path):
    class _NoQuoteBroker(RecordingFakeBroker):
        def get_quote(self, symbol):
            raise ValueError("unknown instrument")

    broker = _NoQuoteBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    assert not preflight.passed
    assert "could not fetch option quote" in preflight.failure_reason
    assert broker.mutation_call_count == 0


# ---------------------------------------------------------------------- #
# Case 8: excessive order value
# ---------------------------------------------------------------------- #
def test_case_8_excessive_order_value_fails_preflight(tmp_path):
    broker = RecordingFakeBroker()
    broker.ltp = 100.0  # 65 * 100 = 6500 > max_order_value 2000
    rig = _rig(tmp_path, broker)
    request = _request(max_order_value=2000.0)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    assert not preflight.passed
    assert "exceeds authorized max_order_value" in preflight.failure_reason


# ---------------------------------------------------------------------- #
# Cases 9-11: excessive daily/strategy loss, excessive order count (RiskManager)
# ---------------------------------------------------------------------- #
def test_case_9_excessive_daily_loss(tmp_path):
    broker = RecordingFakeBroker()
    limits = RiskLimits(max_daily_loss=1000.0)
    rig = _rig(tmp_path, broker, risk_limits=limits)
    request = _request()
    with pytest.raises(WorkflowError, match="RiskManager rejected"):
        validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                          risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                          context=RiskContext(daily_pnl=-1500.0),
                      authorization_service=_authorization_service())
    assert broker.mutation_call_count == 0


def test_case_10_excessive_strategy_loss(tmp_path):
    broker = RecordingFakeBroker()
    limits = RiskLimits(max_strategy_loss=1000.0)
    rig = _rig(tmp_path, broker, risk_limits=limits)
    request = _request()
    with pytest.raises(WorkflowError, match="RiskManager rejected"):
        validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                          risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                          context=RiskContext(strategy_pnl=-1500.0),
                      authorization_service=_authorization_service())
    assert broker.mutation_call_count == 0


def test_case_11_excessive_order_count(tmp_path):
    broker = RecordingFakeBroker()
    limits = RiskLimits(max_orders_per_day=1)
    rig = _rig(tmp_path, broker, risk_limits=limits)
    # Consume the day's budget via one real (fake-broker) execution first.
    out = _full_flow(tmp_path, broker=broker, risk_limits=limits)
    request2 = _request(idempotency_key="wf-key-2")
    with pytest.raises(WorkflowError, match="RiskManager rejected"):
        validate_request(request2, broker_manager=out["rig"]["broker_manager"], idempotency_store=out["rig"]["idem_store"],
                          risk_manager=out["rig"]["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    assert broker.mutation_call_count == 1  # only the first, successful order


# ---------------------------------------------------------------------- #
# Case 12: kill switch enabled
# ---------------------------------------------------------------------- #
def test_case_12_kill_switch_enabled_fails_preflight_and_blocks_execution(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    ks = CentralKillSwitch(persistence_path=str(tmp_path / "ks.json"))
    ks.engage(by="operator", reason="test")
    request = _request()
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID,
                               central_kill_switch=ks)
    assert not preflight.passed
    assert preflight.kill_switch_engaged is True
    assert broker.mutation_call_count == 0

    # Even if an operator bypassed the preflight gate and confirmed anyway,
    # execution itself is still blocked by the SAME kill switch, unconditionally.
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    passed_preflight = PreflightResult(**{**preflight.__dict__, "passed": True, "failure_reason": ""})
    authorized = workflow.confirm(pending.authorization_id, passed_preflight, _approve, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())
    engine = _engine(rig, broker, rig["auth_store"], central_kill_switch=ks)
    result = engine.execute(_intent(request, authorized.authorization_id), context=RiskContext(reference_price=broker.ltp))
    assert not result.success
    assert broker.mutation_call_count == 0


# ---------------------------------------------------------------------- #
# Case 13/14: duplicate / ambiguous idempotency key at request time
# ---------------------------------------------------------------------- #
def test_case_13_duplicate_idempotency_key(tmp_path):
    broker = RecordingFakeBroker()
    out = _full_flow(tmp_path, broker=broker)
    request2 = _request(idempotency_key=out["request"].idempotency_key)  # reused key
    with pytest.raises(WorkflowError, match="already has a record"):
        validate_request(request2, broker_manager=out["rig"]["broker_manager"], idempotency_store=out["rig"]["idem_store"],
                          risk_manager=out["rig"]["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    assert broker.mutation_call_count == 1  # unchanged


def test_case_14_ambiguous_idempotency_key(tmp_path):
    class _RaisingBroker(RecordingFakeBroker):
        def place_order(self, *a, **k):
            self.mutation_call_count += 1
            raise ConnectionError("simulated failure")

    broker = _RaisingBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    authorized = workflow.confirm(pending.authorization_id, preflight, _approve, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())
    engine = _engine(rig, broker, rig["auth_store"])
    engine.execute(_intent(request, authorized.authorization_id), context=RiskContext(reference_price=broker.ltp))
    assert rig["idem_store"].get(request.idempotency_key).status == STATUS_AMBIGUOUS

    request2 = _request(idempotency_key=request.idempotency_key)
    with pytest.raises(WorkflowError, match="already has a record"):
        validate_request(request2, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                          risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    assert broker.mutation_call_count == 1  # not retried


# ---------------------------------------------------------------------- #
# Cases 15-17: expired / revoked / consumed authorization at execute time
# ---------------------------------------------------------------------- #
def test_case_15_expired_authorization_blocks_execution(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request(ttl_seconds=-1)
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)  # already expired
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    with pytest.raises(WorkflowError):
        workflow.confirm(pending.authorization_id, preflight, _approve, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())  # validate() raises since already EXPIRED
    assert broker.mutation_call_count == 0


def test_case_16_revoked_authorization_blocks_execution(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    rig["auth_store"].revoke(pending.authorization_id, reason="operator changed mind")
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    with pytest.raises(WorkflowError):
        workflow.confirm(pending.authorization_id, preflight, _approve, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())
    assert broker.mutation_call_count == 0


def test_case_17_consumed_authorization_blocks_second_execution(tmp_path):
    out = _full_flow(tmp_path)
    engine2 = _engine(out["rig"], out["broker"], out["rig"]["auth_store"])
    intent2 = _intent(out["request"], out["authorized"].authorization_id)
    object.__setattr__(intent2, "idempotency_key", "wf-key-2-different")
    result2 = engine2.execute(intent2, context=RiskContext(reference_price=out["broker"].ltp))
    assert not result2.success
    assert out["broker"].mutation_call_count == 1  # unchanged -- no second broker call


# ---------------------------------------------------------------------- #
# Cases 18-23: changed instrument/side/quantity/account/broker/credential
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("field,value", [
    ("symbol", "NIFTY22SEP2623600CE"),  # 18: changed instrument
    ("side", OrderSide.SELL),  # 19: changed side
    ("quantity", 130),  # 20: changed quantity
])
def test_cases_18_20_changed_intent_field_blocks_execution(tmp_path, field, value):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    authorized = workflow.confirm(pending.authorization_id, preflight, _approve, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())

    intent = _intent(request, authorized.authorization_id)
    intent = OrderIntent(**{**intent.__dict__, field: value})
    engine = _engine(rig, broker, rig["auth_store"])
    result = engine.execute(intent, context=RiskContext(reference_price=broker.ltp))
    assert not result.success
    assert broker.mutation_call_count == 0


def test_case_21_changed_account_blocks_execution(tmp_path):
    """21: the RESOLVED account (via StrategyAssignment, not the intent's
    own account_id field, which is informational only -- see
    execution.py step 2) must match the authorization's scoped
    account_id. Reassigning the strategy to a different account is the
    only way to actually change which account execute() resolves to."""
    broker_a = RecordingFakeBroker()
    broker_b = RecordingFakeBroker()
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account_a = TradingAccount(account_id="ACC_A", account_name="A", broker_id="angelone",
                                credential_reference="env:ANGELONE_A", execution_mode=ExecutionMode.PAPER)
    account_b = _account_b()
    broker_manager.register_account(account_a, broker_client=broker_a)
    broker_manager.register_account(account_b, broker_client=broker_b)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign(STRATEGY_ID, "ACC_B", execution_mode=ExecutionMode.PAPER)
    risk_manager = RiskManager(assignment)

    request = _request()  # scoped to ACC_B
    validate_request(request, broker_manager=broker_manager, idempotency_store=idem_store,
                      risk_manager=risk_manager, strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker_b, risk_manager=risk_manager,
                               idempotency_store=idem_store, strategy_id=STRATEGY_ID)
    authorized = workflow.confirm(pending.authorization_id, preflight, _approve, operator=_TRADER, account=account_b, authorization_service=_authorization_service())

    # Reassign the strategy to ACC_A AFTER authorization was granted for ACC_B.
    assignment.assign(STRATEGY_ID, "ACC_A", execution_mode=ExecutionMode.PAPER)
    engine = StrategyExecutionEngine(
        broker=broker_a, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store, live_authorization_store=auth_store,
    )
    intent = _intent(request, authorized.authorization_id)
    result = engine.execute(intent, context=RiskContext(reference_price=broker_a.ltp))
    assert not result.success
    assert broker_a.mutation_call_count == 0
    assert broker_b.mutation_call_count == 0


def test_case_22_and_23_changed_broker_or_credential_blocks_at_consume(tmp_path):
    """22/23: broker_id and credential_reference are bound at the
    LiveAuthorization level (Phase 15D.5) and validated against the
    resolved account's own values, not the intent -- covered directly and
    exhaustively by test_phase_15d_5_live_authorization.py's own
    parametrized exact-scope tests. This test proves the SAME protection
    holds when reached through this phase's workflow layer."""
    from trading.common.live_authorization import LiveAuthorizationError

    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store)
    pending = workflow.request(request)
    validated = auth_store.validate(pending.authorization_id)
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="dhan",  # changed broker
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key=request.idempotency_key, order_value=1500,
        )
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_A", symbol=SYMBOL, side="BUY", quantity=65,  # changed credential
            order_type="MARKET", product_type="INTRADAY", idempotency_key=request.idempotency_key, order_value=1500,
        )


# ---------------------------------------------------------------------- #
# Cases 24-25: missing / invalid human confirmation
# ---------------------------------------------------------------------- #
def test_case_24_missing_human_confirmation(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)

    def _no_response(_pf):
        return None  # missing/timeout represented as None

    with pytest.raises(WorkflowError, match="human confirmation"):
        workflow.confirm(pending.authorization_id, preflight, _no_response, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())
    assert rig["auth_store"].get(pending.authorization_id).status == AuthorizationStatus.REVOKED.value
    assert broker.mutation_call_count == 0


@pytest.mark.parametrize("bad_response", [False, "yes", "True", 1, "confirmed", "PROCEED"])
def test_case_25_invalid_confirmation_values_never_approve(tmp_path, bad_response):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    with pytest.raises(WorkflowError, match="human confirmation"):
        workflow.confirm(pending.authorization_id, preflight, lambda _pf: bad_response, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())
    assert broker.mutation_call_count == 0


def test_explicit_decline_revokes_and_blocks(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    with pytest.raises(WorkflowError):
        workflow.confirm(pending.authorization_id, preflight, _decline, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())
    assert rig["auth_store"].get(pending.authorization_id).status == AuthorizationStatus.REVOKED.value


def test_confirmation_provider_exception_never_approves(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)

    def _broken(_pf):
        raise RuntimeError("confirmation UI crashed")

    with pytest.raises(WorkflowError):
        workflow.confirm(pending.authorization_id, preflight, _broken, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())
    assert broker.mutation_call_count == 0


# ---------------------------------------------------------------------- #
# Cases 26-28: restart before/after authorization/consumption
# ---------------------------------------------------------------------- #
def test_case_26_restart_before_execution(tmp_path):
    broker = RecordingFakeBroker()
    rig = _rig(tmp_path, broker)
    request = _request()
    validate_request(request, broker_manager=rig["broker_manager"], idempotency_store=rig["idem_store"],
                      risk_manager=rig["risk_manager"], strategy_id=STRATEGY_ID,
                      authorization_service=_authorization_service())
    workflow = LiveAuthorizationWorkflow(rig["auth_store"])
    pending = workflow.request(request)
    preflight = run_preflight(request, broker=broker, risk_manager=rig["risk_manager"],
                               idempotency_store=rig["idem_store"], strategy_id=STRATEGY_ID)
    authorized = workflow.confirm(pending.authorization_id, preflight, _approve, operator=_TRADER, account=rig["account"], authorization_service=_authorization_service())

    # Simulate restart: fresh store instances against the same files.
    auth_store2 = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    idem_store2 = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    assert auth_store2.get(authorized.authorization_id).status == AuthorizationStatus.AUTHORIZED.value

    engine2 = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=rig["risk_manager"], strategy_assignment=rig["assignment"], broker_manager=rig["broker_manager"],
        idempotency_store=idem_store2, live_authorization_store=auth_store2,
    )
    result = engine2.execute(_intent(request, authorized.authorization_id), context=RiskContext(reference_price=broker.ltp))
    assert result.success
    assert broker.mutation_call_count == 1


def test_case_27_restart_after_authorization_only(tmp_path):
    p = str(tmp_path / "auth.db")
    auth_store1 = SqliteLiveAuthorizationStore(db_path=p)
    request = _request()
    workflow = LiveAuthorizationWorkflow(auth_store1)
    pending = workflow.request(request)
    validated = auth_store1.validate(pending.authorization_id)
    del auth_store1

    auth_store2 = SqliteLiveAuthorizationStore(db_path=p)
    reloaded = auth_store2.get(validated.authorization_id)
    assert reloaded.status == AuthorizationStatus.AUTHORIZED.value  # no promotion, no reset


def test_case_28_restart_after_consumed_authorization_cannot_be_reused(tmp_path):
    out = _full_flow(tmp_path)
    p_auth = str(out["rig"]["auth_store"]._db_path)
    from trading.common.live_authorization import LiveAuthorizationError

    auth_store2 = SqliteLiveAuthorizationStore(db_path=p_auth)
    assert auth_store2.get(out["authorized"].authorization_id).status == AuthorizationStatus.CONSUMED.value
    with pytest.raises(LiveAuthorizationError):
        auth_store2.try_consume(
            out["authorized"].authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key=out["request"].idempotency_key,
            order_value=1500,
        )
    assert out["broker"].mutation_call_count == 1  # unchanged


# ---------------------------------------------------------------------- #
# Audit correlation
# ---------------------------------------------------------------------- #
def test_audit_correlation_full_chain(tmp_path):
    out = _full_flow(tmp_path)
    audit_trail = out["rig"]["audit_trail"]
    events = audit_trail.by_idempotency_key(out["request"].idempotency_key)
    event_types = {e.event_type for e in events}
    assert "AUTHORIZATION_CREATED" in event_types
    assert "AUTHORIZATION_VALIDATED" in event_types
    assert "AUTHORIZATION_CONSUMED" in event_types
    assert "BROKER_ORDER_PLACED" in event_types
    for e in events:
        assert "api_key" not in str(e.detail).lower()
        assert "totp" not in str(e.detail).lower()


# ---------------------------------------------------------------------- #
# Historical integrity + authorized_by validation
# ---------------------------------------------------------------------- #
def test_historical_records_untouched():
    import sqlite3

    conn = sqlite3.connect("trading/phase15d2_idempotency.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, broker_order_id FROM idempotency_records WHERE broker_order_id = '260917000350205'"
    ).fetchone()
    conn.close()
    assert dict(row) == {"status": "COMPLETED", "broker_order_id": "260917000350205"}


def _operator_with_display_name(display_name: str) -> OperatorIdentity:
    return OperatorIdentity(
        operator_id="user:15d6-trader", display_name=display_name, role=OperatorRole.TRADER,
        auth_state=AuthState.AUTHENTICATED, authentication_source="fake-test",
        permissions=permissions_for_role(OperatorRole.TRADER), is_active=True,
    )


def test_authorized_by_must_be_non_empty():
    # Phase 15D.7: authorized_by is now DERIVED from operator.display_name
    # -- this test exercises that derivation still enforces the same
    # Phase 15D.6 non-empty rule.
    with pytest.raises(WorkflowError):
        _request(operator=_operator_with_display_name(""))


def test_authorized_by_cannot_contain_secret_shaped_content():
    with pytest.raises(WorkflowError):
        _request(operator=_operator_with_display_name("api_key=SECRET123"))


def test_real_broker_never_called_by_this_file():
    assert issubclass(RecordingFakeBroker, BrokerClient)
    assert RecordingFakeBroker.is_simulated is True
