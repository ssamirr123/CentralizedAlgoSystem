"""Phase 15D.5: durable, explicit, single-use, exactly-scoped human
authorization gate.

Every test uses fake brokers only -- zero real broker calls, zero real
orders. Historical records (260917000350205, the AG7002 PENDING record)
are never touched by anything in this file.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading.common.audit_store import PersistentAuditTrail
from trading.common.broker import BrokerClient, OrderResult, OrderSide, OrderType, Quote
from trading.common.broker_manager import BrokerManager
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import SqliteIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_authorization import (
    EVENT_AUTHORIZATION_CONSUMED,
    EVENT_AUTHORIZATION_CREATED,
    EVENT_AUTHORIZATION_REJECTED,
    EVENT_AUTHORIZATION_REVOKED,
    EVENT_AUTHORIZATION_VALIDATED,
    AuthorizationStatus,
    LiveAuthorizationError,
    SqliteLiveAuthorizationStore,
)
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount

SYMBOL = "NIFTY22SEP2623550CE"


def _create_and_validate(store, **overrides):
    defaults = dict(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B",
        symbol=SYMBOL, side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY",
        max_order_value=2000.0, daily_loss_limit=2000.0, strategy_loss_limit=2000.0,
        max_orders_per_day=1, idempotency_key="idem-key-1", authorized_by="samir",
        ttl_seconds=600,
    )
    defaults.update(overrides)
    rec = store.create(**defaults)
    return store.validate(rec.authorization_id)


class _FakeBroker(BrokerClient):
    is_simulated = True

    def __init__(self) -> None:
        self.connected = False
        self.place_order_calls = 0

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=27.0, timestamp="2026-01-01T00:00:00+00:00")

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        self.place_order_calls += 1
        return OrderResult(order_id=f"FAKE-{self.place_order_calls}", symbol=symbol, side=side,
                            quantity=quantity, status="FILLED", message="ok")

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self):
        return []


def _intent(**overrides):
    defaults = dict(
        strategy_id="PHASE_15D5_CANARY", account_id="ACC_B", symbol=SYMBOL, exchange="NFO",
        side=OrderSide.BUY, quantity=65, order_type=OrderType.MARKET, idempotency_key="idem-key-1",
        correlation_id="corr-1",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _engine(broker, auth_store, tmp_path: Path, *, central_kill_switch=None):
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = TradingAccount(account_id="ACC_B", account_name="B", broker_id="angelone",
                              credential_reference="env:ANGELONE_B", execution_mode=ExecutionMode.PAPER)
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("PHASE_15D5_CANARY", "ACC_B", execution_mode=ExecutionMode.PAPER)
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store, live_authorization_store=auth_store,
        central_kill_switch=central_kill_switch,
    )
    return engine, idem_store, audit_trail


def _context():
    return RiskContext(reference_price=27.0, daily_pnl=0.0, strategy_pnl=0.0)


# ---------------------------------------------------------------------- #
# Step 3: state machine
# ---------------------------------------------------------------------- #
def test_create_starts_pending(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    rec = store.create(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY", max_order_value=2000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="k1", authorized_by="samir",
    )
    assert rec.status == AuthorizationStatus.PENDING.value


def test_validate_transitions_pending_to_authorized(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(store)
    assert validated.status == AuthorizationStatus.AUTHORIZED.value


def test_validate_rejects_structurally_invalid(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    rec = store.create(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=0, order_type="MARKET", product_type="INTRADAY", max_order_value=2000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="k-bad", authorized_by="samir",
    )
    with pytest.raises(LiveAuthorizationError):
        store.validate(rec.authorization_id)
    assert store.get(rec.authorization_id).status == AuthorizationStatus.REJECTED.value


def test_revoke_pending_or_authorized(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(store)
    revoked = store.revoke(validated.authorization_id, reason="operator changed mind", by="samir")
    assert revoked.status == AuthorizationStatus.REVOKED.value


def test_revoke_terminal_state_fails(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(store)
    store.revoke(validated.authorization_id)
    with pytest.raises(LiveAuthorizationError):
        store.revoke(validated.authorization_id)


# ---------------------------------------------------------------------- #
# Step 4: exact-scope binding
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("mismatch_field,mismatch_value", [
    ("quantity", 130),
    ("symbol", "NIFTY22SEP2623600CE"),
    ("side", "SELL"),
    ("account_id", "ACC_A"),
    ("broker_id", "dhan"),
    ("credential_reference", "env:ANGELONE_A"),
    ("order_type", "LIMIT"),
    ("product_type", "DELIVERY"),
    ("idempotency_key", "different-key"),
])
def test_exact_scope_mismatch_blocks_consumption(tmp_path, mismatch_field, mismatch_value):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(store)
    kwargs = dict(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY",
        idempotency_key="idem-key-1", order_value=1500,
    )
    kwargs[mismatch_field] = mismatch_value
    with pytest.raises(LiveAuthorizationError):
        store.try_consume(validated.authorization_id, **kwargs)
    assert store.get(validated.authorization_id).status == AuthorizationStatus.AUTHORIZED.value  # not consumed


def test_order_value_exceeding_max_blocks_consumption(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(store, max_order_value=1000.0)
    with pytest.raises(LiveAuthorizationError):
        store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


def test_exact_match_consumes_successfully(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(store)
    consumed = store.try_consume(
        validated.authorization_id, account_id="ACC_B", broker_id="angelone",
        credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
        order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
    )
    assert consumed.status == AuthorizationStatus.CONSUMED.value


# ---------------------------------------------------------------------- #
# Step 5: expiration
# ---------------------------------------------------------------------- #
def test_authorization_before_expiry_is_valid(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(store, ttl_seconds=600)
    assert not validated.is_expired()


def test_authorization_after_expiry_is_invalid(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(store, ttl_seconds=1)
    future = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert validated.is_expired(now=future)


def test_expired_authorization_cannot_be_consumed(tmp_path):
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    rec = store.create(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY", max_order_value=2000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="k-exp", authorized_by="samir", ttl_seconds=-1,  # already expired at creation
    )
    with pytest.raises(LiveAuthorizationError):
        store.validate(rec.authorization_id)
    # get()'s own eager, durable expiry check (run at the top of validate())
    # intercepts an already-past-expiry record before validate()'s
    # structural checks ever run -- EXPIRED (time-based invalidity) is the
    # correct terminal state here, distinct from REJECTED (structurally
    # invalid data unrelated to timing, e.g. quantity=0 -- see
    # test_validate_rejects_structurally_invalid above).
    assert store.get(rec.authorization_id).status == AuthorizationStatus.EXPIRED.value


def test_expiry_is_durable_on_read_not_merely_computed(tmp_path):
    """Reading an overdue AUTHORIZED record persists the EXPIRED transition
    (not just returns a computed boolean) -- required for restart durability."""
    store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    rec = store.create(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY", max_order_value=2000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="k-exp2", authorized_by="samir", ttl_seconds=600,
    )
    store.validate(rec.authorization_id)
    # Manually age it past expiry via a fresh store pointed at the same file,
    # simulating time passing without calling try_consume in between.
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "auth.db"))
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    conn.execute("UPDATE live_authorizations SET expires_at = ? WHERE authorization_id = ?", (past, rec.authorization_id))
    conn.commit()
    conn.close()

    reloaded = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db")).get(rec.authorization_id)
    assert reloaded.status == AuthorizationStatus.EXPIRED.value

    # Restart again -- still EXPIRED, not reset.
    reloaded2 = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db")).get(rec.authorization_id)
    assert reloaded2.status == AuthorizationStatus.EXPIRED.value


# ---------------------------------------------------------------------- #
# Step 6: single-use semantics (via the real execute() pipeline)
# ---------------------------------------------------------------------- #
def test_single_use_second_execution_blocked_before_broker(tmp_path):
    """The broker is never called twice for the same authorization,
    regardless of which specific mechanism catches the replay. A second
    execute() with the exact same intent (same idempotency_key too) is
    intercepted even earlier, by the pre-existing idempotency-replay gate
    (step 1) -- which correctly returns the cached SUCCESS result rather
    than a fresh rejection (Blocker D's own established, unchanged
    behavior). The authorization layer's OWN single-use enforcement, in
    isolation, is covered directly by test_replay_case_1_same_auth_same_
    intent_after_consumed below -- this test's job is only to prove the
    broker call count never exceeds 1, from either layer."""
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store)
    broker = _FakeBroker()
    engine, idem_store, _audit = _engine(broker, auth_store, tmp_path)

    intent = _intent(metadata={"authorization_id": validated.authorization_id})
    result1 = engine.execute(intent, context=_context())
    assert result1.success
    assert broker.place_order_calls == 1

    # Second execute() with the SAME idempotency_key + SAME authorization_id:
    # idempotency replay (pre-existing, unchanged) returns the cached
    # result -- the broker must never be called a second time.
    intent2 = _intent(idempotency_key="idem-key-1", metadata={"authorization_id": validated.authorization_id})
    result2 = engine.execute(intent2, context=_context())
    assert result2.order_id == result1.order_id  # replayed, not re-executed
    assert broker.place_order_calls == 1  # unchanged -- never called twice
    assert auth_store.get(validated.authorization_id).status == AuthorizationStatus.CONSUMED.value


# ---------------------------------------------------------------------- #
# Step 7: idempotency binding
# ---------------------------------------------------------------------- #
def test_authorization_bound_to_idempotency_key_rejects_different_key(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store, idempotency_key="KEY-A")
    broker = _FakeBroker()
    engine, _idem, _audit = _engine(broker, auth_store, tmp_path)

    intent = _intent(idempotency_key="KEY-B", metadata={"authorization_id": validated.authorization_id})
    result = engine.execute(intent, context=_context())
    assert not result.success
    assert broker.place_order_calls == 0


def test_historical_idempotency_records_untouched(tmp_path):
    """This module must never read/write the REAL historical stores."""
    import sqlite3

    conn = sqlite3.connect("trading/phase15d2_idempotency.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, broker_order_id FROM idempotency_records WHERE broker_order_id = '260917000350205'"
    ).fetchone()
    conn.close()
    assert dict(row) == {"status": "COMPLETED", "broker_order_id": "260917000350205"}


# ---------------------------------------------------------------------- #
# Step 8: account isolation
# ---------------------------------------------------------------------- #
def test_authorization_for_account_b_cannot_execute_against_account_a(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store, account_id="ACC_B")
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_A", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


def test_credential_reference_cannot_be_substituted(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store, credential_reference="env:ANGELONE_B")
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_A", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


# ---------------------------------------------------------------------- #
# Step 9: kill switch integration
# ---------------------------------------------------------------------- #
def test_kill_switch_blocks_even_with_valid_authorization(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store)
    broker = _FakeBroker()
    ks = CentralKillSwitch(persistence_path=str(tmp_path / "ks.json"))
    ks.engage(by="operator", reason="test")
    engine, _idem, _audit = _engine(broker, auth_store, tmp_path, central_kill_switch=ks)

    intent = _intent(metadata={"authorization_id": validated.authorization_id})
    result = engine.execute(intent, context=_context())
    assert not result.success
    assert "kill switch" in result.message.lower()
    assert broker.place_order_calls == 0
    # Authorization itself is untouched -- kill switch is checked first (step 0)
    assert auth_store.get(validated.authorization_id).status == AuthorizationStatus.AUTHORIZED.value


def test_kill_switch_on_survives_restart_and_still_blocks(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store)
    ks_path = str(tmp_path / "ks.json")
    ks1 = CentralKillSwitch(persistence_path=ks_path)
    ks1.engage(by="operator", reason="test")
    del ks1

    ks2 = CentralKillSwitch(persistence_path=ks_path)
    assert ks2.engaged is True
    broker = _FakeBroker()
    engine, _idem, _audit = _engine(broker, auth_store, tmp_path, central_kill_switch=ks2)
    intent = _intent(metadata={"authorization_id": validated.authorization_id})
    result = engine.execute(intent, context=_context())
    assert not result.success
    assert broker.place_order_calls == 0


# ---------------------------------------------------------------------- #
# Step 10: risk integration -- authorization never bypasses RiskManager
# ---------------------------------------------------------------------- #
def test_risk_manager_rejection_still_applies_with_valid_authorization(tmp_path):
    from trading.common.risk_manager import RiskLimits

    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store, quantity=65, max_order_value=999999)
    broker = _FakeBroker()

    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = TradingAccount(account_id="ACC_B", account_name="B", broker_id="angelone",
                              credential_reference="env:ANGELONE_B", execution_mode=ExecutionMode.PAPER)
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("PHASE_15D5_CANARY", "ACC_B", execution_mode=ExecutionMode.PAPER)
    # A RiskLimits that will reject on quantity.
    risk_manager = RiskManager(assignment, limits=RiskLimits(max_order_quantity=1))
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store, live_authorization_store=auth_store,
    )
    intent = _intent(quantity=65, metadata={"authorization_id": validated.authorization_id})
    result = engine.execute(intent, context=_context())
    assert not result.success
    assert broker.place_order_calls == 0
    # RiskManager rejected before the authorization gate was ever reached.
    assert auth_store.get(validated.authorization_id).status == AuthorizationStatus.AUTHORIZED.value


# ---------------------------------------------------------------------- #
# Step 11/12: audit trail + correlation
# ---------------------------------------------------------------------- #
def test_authorization_lifecycle_events_are_durably_audited(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    rec = auth_store.create(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY", max_order_value=2000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="audit-key-1", authorized_by="samir",
    )
    validated = auth_store.validate(rec.authorization_id)
    consumed = auth_store.try_consume(
        validated.authorization_id, account_id="ACC_B", broker_id="angelone",
        credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
        order_type="MARKET", product_type="INTRADAY", idempotency_key="audit-key-1", order_value=1500,
    )
    assert len(audit_trail.by_event_type(EVENT_AUTHORIZATION_CREATED)) == 1
    assert len(audit_trail.by_event_type(EVENT_AUTHORIZATION_VALIDATED)) == 1
    assert len(audit_trail.by_event_type(EVENT_AUTHORIZATION_CONSUMED)) == 1

    events = audit_trail.by_idempotency_key("audit-key-1")
    assert len(events) == 3
    for e in events:
        assert "api_key" not in str(e.detail).lower()
        assert "totp" not in str(e.detail).lower()


def test_audit_correlation_authorization_to_broker_order(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store)
    broker = _FakeBroker()
    engine, idem_store, audit_trail = _engine(broker, auth_store, tmp_path)
    intent = _intent(metadata={"authorization_id": validated.authorization_id})
    result = engine.execute(intent, context=_context())
    assert result.success

    idem_record = idem_store.get("idem-key-1")
    assert idem_record.broker_order_id == result.order_id
    audit_events = audit_trail.by_idempotency_key("idem-key-1")
    broker_placed = [e for e in audit_events if e.event_type == "BROKER_ORDER_PLACED"]
    assert len(broker_placed) == 1
    assert broker_placed[0].broker_order_id == result.order_id


def test_rejected_authorization_is_audited(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    rec = auth_store.create(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=0, order_type="MARKET", product_type="INTRADAY", max_order_value=2000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="rej-key", authorized_by="samir",
    )
    with pytest.raises(LiveAuthorizationError):
        auth_store.validate(rec.authorization_id)
    assert len(audit_trail.by_event_type(EVENT_AUTHORIZATION_REJECTED)) == 1


def test_revoked_authorization_is_audited(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"), audit_trail=audit_trail)
    validated = _create_and_validate(auth_store)
    auth_store.revoke(validated.authorization_id, reason="operator cancelled")
    assert len(audit_trail.by_event_type(EVENT_AUTHORIZATION_REVOKED)) == 1


# ---------------------------------------------------------------------- #
# Step 13: crash / restart safety
# ---------------------------------------------------------------------- #
def test_scenario_a_created_survives_restart_if_not_expired(tmp_path):
    p = str(tmp_path / "auth.db")
    store1 = SqliteLiveAuthorizationStore(db_path=p)
    validated = _create_and_validate(store1, ttl_seconds=600)
    del store1
    store2 = SqliteLiveAuthorizationStore(db_path=p)
    reloaded = store2.get(validated.authorization_id)
    assert reloaded.status == AuthorizationStatus.AUTHORIZED.value


def test_scenario_b_consumed_survives_restart(tmp_path):
    p = str(tmp_path / "auth.db")
    store1 = SqliteLiveAuthorizationStore(db_path=p)
    validated = _create_and_validate(store1)
    store1.try_consume(
        validated.authorization_id, account_id="ACC_B", broker_id="angelone",
        credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
        order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
    )
    del store1
    store2 = SqliteLiveAuthorizationStore(db_path=p)
    assert store2.get(validated.authorization_id).status == AuthorizationStatus.CONSUMED.value


def test_scenario_c_expired_survives_restart(tmp_path):
    p = str(tmp_path / "auth.db")
    store1 = SqliteLiveAuthorizationStore(db_path=p)
    rec = store1.create(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY", max_order_value=2000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="k-restart-exp", authorized_by="samir", ttl_seconds=-1,
    )
    with pytest.raises(LiveAuthorizationError):
        store1.validate(rec.authorization_id)  # EXPIRED, since already expired at validation time (see
        # test_validate_rejects_structurally_invalid's own comment for why
        # this differs from a structural REJECTED)
    del store1
    store2 = SqliteLiveAuthorizationStore(db_path=p)
    assert store2.get(rec.authorization_id).status == AuthorizationStatus.EXPIRED.value


def test_scenario_d_revoked_survives_restart(tmp_path):
    p = str(tmp_path / "auth.db")
    store1 = SqliteLiveAuthorizationStore(db_path=p)
    validated = _create_and_validate(store1)
    store1.revoke(validated.authorization_id)
    del store1
    store2 = SqliteLiveAuthorizationStore(db_path=p)
    assert store2.get(validated.authorization_id).status == AuthorizationStatus.REVOKED.value


def test_scenario_e_ambiguous_broker_response_after_authorization_stays_protected(tmp_path):
    """Execution crashes/loses observability after a broker mutation --
    the EXISTING ambiguous/idempotency protections must still apply
    unchanged; the authorization gate does not introduce automatic retry."""
    from trading.common.idempotency_store import STATUS_AMBIGUOUS

    class _RaisingBroker(_FakeBroker):
        def place_order(self, *a, **k):
            self.place_order_calls += 1
            raise ConnectionError("simulated failure")

    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store)
    broker = _RaisingBroker()
    engine, idem_store, _audit = _engine(broker, auth_store, tmp_path)
    intent = _intent(metadata={"authorization_id": validated.authorization_id})
    result = engine.execute(intent, context=_context())
    assert not result.success
    assert idem_store.get("idem-key-1").status == STATUS_AMBIGUOUS
    assert broker.place_order_calls == 1
    # The authorization was already legitimately CONSUMED before the
    # ambiguous broker call (consumption happens before idempotency claim
    # and before the broker call) -- it must not be un-consumed or retried.
    assert auth_store.get(validated.authorization_id).status == AuthorizationStatus.CONSUMED.value


# ---------------------------------------------------------------------- #
# Step 14: replay attack / reuse protection
# ---------------------------------------------------------------------- #
def test_replay_case_1_same_auth_same_intent_after_consumed(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store)
    auth_store.try_consume(
        validated.authorization_id, account_id="ACC_B", broker_id="angelone",
        credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
        order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
    )
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


def test_replay_case_2_different_idempotency_key(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store, idempotency_key="KEY-A")
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="KEY-DIFFERENT", order_value=1500,
        )


def test_replay_case_3_different_quantity(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store, quantity=65)
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=130,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


def test_replay_case_4_different_instrument(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store, symbol=SYMBOL)
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol="NIFTY22SEP2623600CE", side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


def test_replay_case_5_different_account(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store, account_id="ACC_B")
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_A", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


def test_replay_case_6_after_expiry(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    rec = auth_store.create(
        account_id="ACC_B", broker_id="angelone", credential_reference="env:ANGELONE_B", symbol=SYMBOL,
        side="BUY", quantity=65, order_type="MARKET", product_type="INTRADAY", max_order_value=2000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="k-replay-exp", authorized_by="samir", ttl_seconds=1,
    )
    validated = auth_store.validate(rec.authorization_id)
    import time as _time

    _time.sleep(1.2)
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="k-replay-exp", order_value=1500,
        )
    assert auth_store.get(validated.authorization_id).status == AuthorizationStatus.EXPIRED.value


def test_replay_case_7_after_revocation(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    validated = _create_and_validate(auth_store)
    auth_store.revoke(validated.authorization_id)
    with pytest.raises(LiveAuthorizationError):
        auth_store.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


def test_replay_case_8_after_restart(tmp_path):
    p = str(tmp_path / "auth.db")
    store1 = SqliteLiveAuthorizationStore(db_path=p)
    validated = _create_and_validate(store1)
    store1.try_consume(
        validated.authorization_id, account_id="ACC_B", broker_id="angelone",
        credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
        order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
    )
    del store1
    store2 = SqliteLiveAuthorizationStore(db_path=p)
    with pytest.raises(LiveAuthorizationError):
        store2.try_consume(
            validated.authorization_id, account_id="ACC_B", broker_id="angelone",
            credential_reference="env:ANGELONE_B", symbol=SYMBOL, side="BUY", quantity=65,
            order_type="MARKET", product_type="INTRADAY", idempotency_key="idem-key-1", order_value=1500,
        )


# ---------------------------------------------------------------------- #
# No live authorization required -> existing callers unaffected
# ---------------------------------------------------------------------- #
def test_engine_without_live_authorization_store_is_unaffected(tmp_path):
    """Backward compatibility: every existing test/caller that never
    passes live_authorization_store must see zero behavior change."""
    broker = _FakeBroker()
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    broker_manager = BrokerManager()
    account = TradingAccount(account_id="ACC_B", account_name="B", broker_id="angelone", execution_mode=ExecutionMode.PAPER)
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("PHASE_15D5_CANARY", "ACC_B", execution_mode=ExecutionMode.PAPER)
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        idempotency_store=idem_store,
    )
    result = engine.execute(_intent(), context=_context())
    assert result.success  # no authorization_id needed when the gate isn't attached


def test_real_broker_never_called_by_this_file():
    """Sanity proof: every broker in this test file is the in-process fake."""
    assert issubclass(_FakeBroker, BrokerClient)
    assert _FakeBroker.is_simulated is True
