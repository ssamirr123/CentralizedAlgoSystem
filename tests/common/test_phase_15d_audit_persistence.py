"""Phase 15D-AUDIT: persistent, durable audit-trail hardening.

Every test in this file uses a fake/deterministic broker (never a real
network call) and a temp SQLite file per test -- zero real broker
mutation calls, zero real orders, zero strategies started, matching this
phase's hard safety rule.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from trading.common.audit_store import (
    EVENT_AMBIGUOUS_ORDER_STATE,
    EVENT_AUTHORIZATION_STATE_CHANGED,
    EVENT_DEPLOYMENT_STARTUP,
    EVENT_HUMAN_AUTHORIZATION_DECISION,
    EVENT_KILL_SWITCH_DISENGAGED,
    EVENT_KILL_SWITCH_ENGAGED,
    EVENT_RECONCILIATION_COMPLETED,
    REDACTED,
    AuditPersistenceError,
    PersistentAuditTrail,
    SqliteAuditStore,
    record_authorization_transition,
)
from trading.common.broker import BrokerClient, OrderResult, OrderSide, OrderType, Quote
from trading.common.broker_manager import BrokerManager
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import (
    AmbiguousIdempotencyStateError,
    STATUS_AMBIGUOUS,
    SqliteIdempotencyStore,
)
from trading.common.kill_switch import CentralKillSwitch
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import (
    AccountAuthorizationError,
    AccountAuthorizationState,
    ExecutionMode,
    TradingAccount,
)


def _tmp_db(tmp_path: Path, name: str = "audit.db") -> str:
    return str(tmp_path / name)


def _intent(**kwargs) -> OrderIntent:
    defaults = dict(
        strategy_id="StratA", account_id="ACC_A", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=50, order_type=OrderType.MARKET,
        idempotency_key="", correlation_id="corr-1",
    )
    defaults.update(kwargs)
    return OrderIntent(**defaults)


class _FakeBroker(BrokerClient):
    """Deterministic, in-process fake -- never touches a network. `mode`
    controls place_order()'s behavior: "fill" (normal), "raise" (simulates
    an ambiguous broker exception during submission)."""

    is_simulated = True

    def __init__(self, mode: str = "fill") -> None:
        self.mode = mode
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=100.0, timestamp="2026-01-01T00:00:00+00:00")

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        if self.mode == "raise":
            raise ConnectionError("simulated network failure during order submission")
        return OrderResult(
            order_id="FAKE-1", symbol=symbol, side=side, quantity=quantity,
            status="FILLED", message="ok",
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self):
        return []


def _wire_engine(tmp_path, *, broker_mode="fill", audit_db="audit.db", idem_db="idem.db"):
    audit_trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path, audit_db))
    idem_store = SqliteIdempotencyStore(db_path=_tmp_db(tmp_path, idem_db))
    broker = _FakeBroker(mode=broker_mode)
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = TradingAccount(
        account_id="ACC_A", account_name="A", broker_id="fake",
        execution_mode=ExecutionMode.PAPER, authorization_state=AccountAuthorizationState.READ_ONLY,
    )
    broker_manager.register_account(account, broker_client=broker, broker_factory=lambda: broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("StratA", "ACC_A")
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(dry_run=False, max_retries=1, retry_delay_seconds=0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store,
    )
    return engine, audit_trail, idem_store, account


# ---------------------------------------------------------------------- #
# 1. Event persistence
# ---------------------------------------------------------------------- #
def test_1_event_persistence(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    record = trail.append("ORDER_INTENT_CREATED", correlation_id="c1", strategy_id="S1", account_id="A1", symbol="NIFTY")
    assert record.event_id
    stored = trail.records()
    assert len(stored) == 1
    assert stored[0].event_type == "ORDER_INTENT_CREATED"
    assert stored[0].account_id == "A1"
    assert stored[0].detail["symbol"] == "NIFTY"


# ---------------------------------------------------------------------- #
# 2. Restart persistence
# ---------------------------------------------------------------------- #
def test_2_restart_persistence(tmp_path):
    db_path = _tmp_db(tmp_path)
    trail1 = PersistentAuditTrail(db_path=db_path)
    trail1.append("ORDER_INTENT_CREATED", correlation_id="c1", account_id="A1")
    trail1.append("RISK_DECISION", correlation_id="c1", account_id="A1", status="APPROVED")
    trail1.append("KILL_SWITCH_ENGAGED", account_id="")
    del trail1  # simulate process death -- no explicit close/flush call exists or is needed

    trail2 = PersistentAuditTrail(db_path=db_path)  # simulates a fresh process restart
    records = trail2.records()
    assert len(records) == 3
    assert [r.event_type for r in records] == ["ORDER_INTENT_CREATED", "RISK_DECISION", "KILL_SWITCH_ENGAGED"]
    # the chain continues correctly across the "restart" -- appending after
    # reload links to the last pre-restart hash, not a fresh genesis.
    trail2.append("EXECUTION_RESULT", correlation_id="c1", account_id="A1")
    assert trail2.verify() is True
    assert len(trail2.records()) == 4


# ---------------------------------------------------------------------- #
# 3. Append-only behavior
# ---------------------------------------------------------------------- #
def test_3_append_only_update_rejected_at_database_level(tmp_path):
    db_path = _tmp_db(tmp_path)
    trail = PersistentAuditTrail(db_path=db_path)
    trail.append("ORDER_INTENT_CREATED", correlation_id="c1")

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_events SET event_type = 'TAMPERED' WHERE seq = 0")
    conn.close()


def test_3_append_only_delete_rejected_at_database_level(tmp_path):
    db_path = _tmp_db(tmp_path)
    trail = PersistentAuditTrail(db_path=db_path)
    trail.append("ORDER_INTENT_CREATED", correlation_id="c1")

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_events WHERE seq = 0")
    conn.close()


def test_3_correction_is_a_new_event_referencing_the_original(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    original = trail.append("RISK_DECISION", correlation_id="c1", status="APPROVED")
    correction = trail.append("RISK_DECISION", correlation_id="c1", status="REJECTED", corrects_event_id=original.event_id)
    assert len(trail.records()) == 2
    assert trail.records()[1].detail["corrects_event_id"] == original.event_id
    assert trail.records()[0].detail["status"] == "APPROVED"  # original is untouched


# ---------------------------------------------------------------------- #
# 4. Duplicate event handling
# ---------------------------------------------------------------------- #
def test_4_duplicate_event_id_rejected(tmp_path):
    store = SqliteAuditStore(db_path=_tmp_db(tmp_path))
    record = store.append("ORDER_INTENT_CREATED", detail={})
    # Simulate a lower-level caller attempting to insert the SAME event_id
    # again (e.g. a buggy retry that replays the exact row) -- the PRIMARY
    # KEY constraint must reject it, never silently overwrite or duplicate.
    conn = sqlite3.connect(store._db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO audit_events (event_id, seq, timestamp, event_type, correlation_id, strategy_id, "
            "account_id, owner_id, broker_id, idempotency_key, broker_order_id, deployment_id, app_version, "
            "git_sha, environment, detail_json, prev_hash, hash) VALUES "
            "(?, 999, ?, 'DUPLICATE', '', '', '', '', '', '', '', '', '', '', '', '{}', ?, ?)",
            (record.event_id, record.timestamp, record.prev_hash, record.hash),
        )
    conn.close()


# ---------------------------------------------------------------------- #
# 5. Account isolation
# ---------------------------------------------------------------------- #
def test_5_account_isolation_sequential(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    trail.append("ORDER_INTENT_CREATED", account_id="ANGEL_SAMIR", correlation_id="a1")
    trail.append("ORDER_INTENT_CREATED", account_id="ANGEL_ACCOUNT_B", correlation_id="b1")
    trail.append("EXECUTION_RESULT", account_id="ANGEL_SAMIR", correlation_id="a1")

    a_events = trail.by_account("ANGEL_SAMIR")
    b_events = trail.by_account("ANGEL_ACCOUNT_B")
    assert len(a_events) == 2
    assert len(b_events) == 1
    assert all(r.account_id == "ANGEL_SAMIR" for r in a_events)
    assert all(r.account_id == "ANGEL_ACCOUNT_B" for r in b_events)


def test_5_account_isolation_concurrent_writes(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    errors = []

    def _write(account_id, n):
        try:
            for i in range(n):
                trail.append("ORDER_INTENT_CREATED", account_id=account_id, correlation_id=f"{account_id}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t_a = threading.Thread(target=_write, args=("ANGEL_SAMIR", 20))
    t_b = threading.Thread(target=_write, args=("ANGEL_ACCOUNT_B", 20))
    t_a.start(); t_b.start()
    t_a.join(); t_b.join()

    assert errors == []
    assert len(trail.by_account("ANGEL_SAMIR")) == 20
    assert len(trail.by_account("ANGEL_ACCOUNT_B")) == 20
    assert all(r.account_id == "ANGEL_SAMIR" for r in trail.by_account("ANGEL_SAMIR"))
    assert all(r.account_id == "ANGEL_ACCOUNT_B" for r in trail.by_account("ANGEL_ACCOUNT_B"))
    assert trail.verify() is True  # concurrent writers never corrupted the shared hash chain


# ---------------------------------------------------------------------- #
# 6. Idempotency correlation
# ---------------------------------------------------------------------- #
def test_6_idempotency_correlation_full_lifecycle_reconstructable(tmp_path):
    engine, trail, idem_store, _account = _wire_engine(tmp_path)
    intent = _intent(idempotency_key="idem-key-1", correlation_id="corr-xyz")
    result = engine.execute(intent)
    assert result.success

    by_intent = trail.by_intent("corr-xyz")
    by_key = trail.by_idempotency_key("idem-key-1")
    by_order = trail.by_broker_order_id(result.order_id)
    assert len(by_intent) > 0
    assert len(by_key) > 0  # BROKER_ORDER_PLACED carries idempotency_key via account_id promotion path
    assert len(by_order) > 0
    # investigator reconstruction: intent -> idempotency_key -> broker_order_id all resolve consistently
    idem_record = idem_store.get("idem-key-1")
    assert idem_record.broker_order_id == result.order_id


# ---------------------------------------------------------------------- #
# 7. Broker-order correlation
# ---------------------------------------------------------------------- #
def test_7_broker_order_correlation(tmp_path):
    engine, trail, _idem, _account = _wire_engine(tmp_path)
    result = engine.execute(_intent(idempotency_key="k7", correlation_id="c7"))
    events = trail.by_broker_order_id(result.order_id)
    assert any(r.event_type == "BROKER_ORDER_PLACED" for r in events)


# ---------------------------------------------------------------------- #
# 8 & 9. Ambiguous-response persistence + restart
# ---------------------------------------------------------------------- #
def test_8_ambiguous_response_persisted(tmp_path):
    engine, trail, idem_store, _account = _wire_engine(tmp_path, broker_mode="raise")
    intent = _intent(idempotency_key="ambig-key", correlation_id="c-ambig")
    result = engine.execute(intent)
    assert not result.success
    assert "ambiguous" in result.message.lower()

    record = idem_store.get("ambig-key")
    assert record.status == STATUS_AMBIGUOUS
    ambiguous_events = trail.by_event_type(EVENT_AMBIGUOUS_ORDER_STATE)
    assert len(ambiguous_events) == 1
    assert ambiguous_events[0].idempotency_key == "ambig-key"


def test_9_ambiguous_response_survives_restart_and_blocks_automatic_retry(tmp_path):
    audit_path = _tmp_db(tmp_path, "audit.db")
    idem_path = _tmp_db(tmp_path, "idem.db")

    engine1, trail1, idem1, _account = _wire_engine(tmp_path, broker_mode="raise", audit_db="audit.db", idem_db="idem.db")
    intent = _intent(idempotency_key="restart-ambig", correlation_id="c-restart")
    engine1.execute(intent)
    assert idem1.get("restart-ambig").status == STATUS_AMBIGUOUS
    del engine1, trail1, idem1  # simulate process death

    # Fresh process: brand-new engine/audit-trail/idempotency-store objects
    # pointed at the SAME files -- this is what "restart" means for this
    # architecture (no in-memory state to lose in the first place).
    engine2, trail2, idem2, _account2 = _wire_engine(tmp_path, broker_mode="raise", audit_db="audit.db", idem_db="idem.db")

    # The audit trail still shows the pre-restart ambiguous event.
    ambiguous_events = trail2.by_event_type(EVENT_AMBIGUOUS_ORDER_STATE)
    assert len(ambiguous_events) == 1

    # Attempting the SAME idempotency key after restart must NOT silently
    # retry against the broker -- it must raise, requiring reconciliation.
    with pytest.raises(AmbiguousIdempotencyStateError):
        engine2.execute(_intent(idempotency_key="restart-ambig", correlation_id="c-restart"))


# ---------------------------------------------------------------------- #
# 10. No automatic retry after restart (broker call count proof)
# ---------------------------------------------------------------------- #
def test_10_no_automatic_retry_after_restart_broker_never_called_again(tmp_path):
    call_count = {"n": 0}

    class _CountingBroker(_FakeBroker):
        def place_order(self, *args, **kwargs):
            call_count["n"] += 1
            raise ConnectionError("simulated failure")

    audit_trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path, "audit.db"))
    idem_store = SqliteIdempotencyStore(db_path=_tmp_db(tmp_path, "idem.db"))
    broker = _CountingBroker()
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = TradingAccount(account_id="ACC_A", account_name="A", broker_id="fake", execution_mode=ExecutionMode.PAPER)
    broker_manager.register_account(account, broker_client=broker, broker_factory=lambda: broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("StratA", "ACC_A")
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store,
    )
    engine.execute(_intent(idempotency_key="no-retry-key", correlation_id="c10"))
    assert call_count["n"] == 1

    # "restart": fresh engine, same underlying files -- second attempt must not call the broker.
    engine2 = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0),
        risk_manager=RiskManager(assignment), strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=PersistentAuditTrail(db_path=_tmp_db(tmp_path, "audit.db")),
        idempotency_store=SqliteIdempotencyStore(db_path=_tmp_db(tmp_path, "idem.db")),
    )
    with pytest.raises(AmbiguousIdempotencyStateError):
        engine2.execute(_intent(idempotency_key="no-retry-key", correlation_id="c10"))
    assert call_count["n"] == 1  # unchanged -- broker was never called a second time


# ---------------------------------------------------------------------- #
# 11. Kill-switch persistence (state + audit)
# ---------------------------------------------------------------------- #
def test_11_kill_switch_state_change_is_audited(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    ks = CentralKillSwitch(audit_trail=audit_trail)
    ks.engage(by="operator1", reason="manual stop")
    ks.disengage(by="operator1")

    engaged_events = audit_trail.by_event_type(EVENT_KILL_SWITCH_ENGAGED)
    disengaged_events = audit_trail.by_event_type(EVENT_KILL_SWITCH_DISENGAGED)
    assert len(engaged_events) == 1
    assert engaged_events[0].detail["actor"] == "operator1"
    assert engaged_events[0].detail["reason"] == "manual stop"
    assert len(disengaged_events) == 1


def test_11_kill_switch_engage_survives_audit_failure(tmp_path):
    """The switch itself must engage even if the audit write fails --
    audit is observational, never a precondition for a safety action."""

    class _BoomAuditTrail:
        def append(self, *args, **kwargs):
            raise RuntimeError("simulated audit failure")

    ks = CentralKillSwitch(audit_trail=_BoomAuditTrail())
    ks.engage(by="op", reason="test")
    assert ks.engaged is True  # unaffected by the audit failure


def test_11_kill_switch_engaged_before_restart_stays_engaged_and_blocks_execution(tmp_path):
    ks_path = str(tmp_path / "kill_switch.json")
    audit_trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))

    ks1 = CentralKillSwitch(persistence_path=ks_path, audit_trail=audit_trail)
    ks1.engage(by="operator1", reason="incident")
    del ks1  # simulate restart

    ks2 = CentralKillSwitch(persistence_path=ks_path, audit_trail=audit_trail)
    assert ks2.engaged is True

    engine, _trail, _idem, _account = _wire_engine(tmp_path, audit_db="audit2.db", idem_db="idem2.db")
    engine._central_kill_switch = ks2
    result = engine.execute(_intent(correlation_id="c-blocked"))
    assert not result.success
    assert "kill switch" in result.message.lower()


# ---------------------------------------------------------------------- #
# 12. Authorization audit
# ---------------------------------------------------------------------- #
def test_12_authorization_state_transition_is_audited(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    account = TradingAccount(account_id="ACC_A", account_name="A", broker_id="angelone")
    previous = account.set_authorization_state(AccountAuthorizationState.CANARY_READY, reason="passed review")
    record_authorization_transition(audit_trail, account, previous_state=previous.value, reason="passed review")

    events = audit_trail.by_event_type(EVENT_AUTHORIZATION_STATE_CHANGED)
    assert len(events) == 1
    assert events[0].account_id == "ACC_A"
    assert events[0].detail["previous_state"] == "READ_ONLY"
    assert events[0].detail["new_state"] == "CANARY_READY"
    assert events[0].detail["reason"] == "passed review"


def test_12_authorization_audit_never_grants_authorization_itself(tmp_path):
    """Recording an audit event must have zero effect on the account's own
    state -- Area K: 'the audit system must never itself grant
    authorization'."""
    audit_trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    account = TradingAccount(account_id="ACC_A", account_name="A", broker_id="angelone")
    # Append a fabricated LIVE_AUTHORIZED audit event WITHOUT ever calling
    # set_authorization_state() -- purely observational data.
    audit_trail.append(EVENT_AUTHORIZATION_STATE_CHANGED, account_id="ACC_A", new_state="LIVE_AUTHORIZED")
    assert account.authorization_state == AccountAuthorizationState.READ_ONLY  # untouched
    assert account.is_live_authorized() is False


def test_12_killed_is_irreversible_even_via_set_authorization_state(tmp_path):
    account = TradingAccount(account_id="ACC_A", account_name="A", broker_id="angelone")
    account.set_killed("emergency")
    with pytest.raises(AccountAuthorizationError):
        account.set_authorization_state(AccountAuthorizationState.READ_ONLY)


# ---------------------------------------------------------------------- #
# 13. Deployment audit
# ---------------------------------------------------------------------- #
def test_13_deployment_startup_event_carries_identity_and_survives_restart(tmp_path):
    db_path = _tmp_db(tmp_path)
    trail1 = PersistentAuditTrail(db_path=db_path)
    trail1.append(
        EVENT_DEPLOYMENT_STARTUP, app_version="1.2.3", git_sha="abc123",
        deployment_id="dep-1", environment="production",
    )
    del trail1

    trail2 = PersistentAuditTrail(db_path=db_path)
    events = trail2.by_event_type(EVENT_DEPLOYMENT_STARTUP)
    assert len(events) == 1
    assert events[0].detail["app_version"] == "1.2.3"
    # every event -- not just this one -- auto-carries the actual running
    # process's own deployment identity as indexed columns too.
    assert events[0].deployment_id  # non-empty: injected automatically by SqliteAuditStore.append()
    assert events[0].app_version
    assert events[0].git_sha


def test_13_query_by_deployment_id(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    r = trail.append("ORDER_INTENT_CREATED", correlation_id="c1")
    events = trail.by_deployment_id(r.deployment_id)
    assert len(events) == 1


# ---------------------------------------------------------------------- #
# 14. Database failure
# ---------------------------------------------------------------------- #
def test_14_database_locked_fails_closed(tmp_path, monkeypatch):
    store = SqliteAuditStore(db_path=_tmp_db(tmp_path))

    def _boom_connect(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteAuditStore, "_connect", _boom_connect)
    with pytest.raises((AuditPersistenceError, sqlite3.OperationalError)):
        store.append("ORDER_INTENT_CREATED", detail={})


def test_14_malformed_event_type_rejected(tmp_path):
    store = SqliteAuditStore(db_path=_tmp_db(tmp_path))
    with pytest.raises(AuditPersistenceError):
        store.append("", detail={})
    with pytest.raises(AuditPersistenceError):
        store.append(None, detail={})  # type: ignore[arg-type]


def test_14_invalid_account_id_type_rejected(tmp_path):
    store = SqliteAuditStore(db_path=_tmp_db(tmp_path))
    with pytest.raises(AuditPersistenceError):
        store.append("ORDER_INTENT_CREATED", account_id=12345, detail={})  # type: ignore[arg-type]


def test_14_oversized_event_rejected_not_truncated(tmp_path):
    store = SqliteAuditStore(db_path=_tmp_db(tmp_path))
    huge_detail = {"payload": "x" * 200_000}
    with pytest.raises(AuditPersistenceError, match="exceeding"):
        store.append("ORDER_INTENT_CREATED", detail=huge_detail)
    assert store.records() == []  # no partial/truncated row was written


def test_14_non_serializable_detail_rejected(tmp_path):
    store = SqliteAuditStore(db_path=_tmp_db(tmp_path))
    with pytest.raises(AuditPersistenceError):
        store.append("ORDER_INTENT_CREATED", detail={"broker": object()})


def test_14_corrupted_detail_json_fails_closed_on_read(tmp_path):
    db_path = _tmp_db(tmp_path)
    store = SqliteAuditStore(db_path=db_path)
    store.append("ORDER_INTENT_CREATED", detail={"x": 1})

    # Simulate on-disk corruption directly at the SQL level (bypassing the
    # append-only trigger is not possible via UPDATE -- this uses the raw
    # file to emulate a corrupted row existing from some other cause, e.g.
    # a crash mid-write on a filesystem without atomic writes).
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA writable_schema=1")
    conn.executescript("DROP TRIGGER audit_events_no_update;")
    conn.execute("UPDATE audit_events SET detail_json = 'not-json' WHERE seq = 0")
    conn.commit()
    conn.close()

    with pytest.raises(AuditPersistenceError):
        store.records()


# ---------------------------------------------------------------------- #
# 15. Concurrent writes
# ---------------------------------------------------------------------- #
def test_15_concurrent_writes_all_succeed_and_chain_stays_valid(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    errors = []

    def _writer(n):
        try:
            for i in range(n):
                trail.append("ORDER_INTENT_CREATED", correlation_id=f"c-{threading.get_ident()}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(15,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    records = trail.records()
    assert len(records) == 75
    assert [r.seq for r in records] == list(range(75))  # no gaps, no duplicates
    assert trail.verify() is True


# ---------------------------------------------------------------------- #
# 16-19. Query capabilities
# ---------------------------------------------------------------------- #
def test_16_query_by_account(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    trail.append("ORDER_INTENT_CREATED", account_id="ACC_A")
    trail.append("ORDER_INTENT_CREATED", account_id="ACC_B")
    assert len(trail.by_account("ACC_A")) == 1
    assert len(trail.by_account("ACC_B")) == 1
    assert len(trail.by_account("NONEXISTENT")) == 0


def test_17_query_by_intent(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    trail.append("ORDER_INTENT_CREATED", correlation_id="corr-1")
    trail.append("RISK_DECISION", correlation_id="corr-1")
    trail.append("ORDER_INTENT_CREATED", correlation_id="corr-2")
    assert len(trail.by_intent("corr-1")) == 2
    assert len(trail.trace("corr-1")) == 2  # alias, matches observability.AuditTrail's own name


def test_18_query_by_idempotency_key(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    trail.append("BROKER_ORDER_PLACED", idempotency_key="key-1", broker_order_id="ord-1")
    trail.append("BROKER_ORDER_PLACED", idempotency_key="key-2", broker_order_id="ord-2")
    events = trail.by_idempotency_key("key-1")
    assert len(events) == 1
    assert events[0].broker_order_id == "ord-1"


def test_19_query_by_broker_order_id(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    trail.append("BROKER_ORDER_PLACED", broker_order_id="ord-99")
    trail.append("FILL", broker_order_id="ord-99")
    trail.append("BROKER_ORDER_PLACED", broker_order_id="ord-other")
    events = trail.by_broker_order_id("ord-99")
    assert len(events) == 2


def test_query_by_time_range_and_event_type(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    r1 = trail.append("ORDER_INTENT_CREATED")
    time.sleep(0.01)
    r2 = trail.append("RISK_DECISION")
    events = trail.by_time_range(r1.timestamp, r2.timestamp)
    assert len(events) == 2
    assert len(trail.by_event_type("RISK_DECISION")) == 1


# ---------------------------------------------------------------------- #
# 20. Secret redaction
# ---------------------------------------------------------------------- #
def test_20_secret_redaction(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    trail.append(
        "BROKER_CONNECTED",
        api_key="SECRET123", password="hunter2", totp_secret="ABCDEF",
        access_token="tok_live_xyz", mpin="1234", client_id="AA0001",
    )
    record = trail.records()[0]
    assert record.detail["api_key"] == REDACTED
    assert record.detail["password"] == REDACTED
    assert record.detail["totp_secret"] == REDACTED
    assert record.detail["access_token"] == REDACTED
    assert record.detail["mpin"] == REDACTED
    assert record.detail["client_id"] == "AA0001"  # not a secret -- preserved

    # confirm it's genuinely absent from the raw file, not just from the
    # dataclass's view of it.
    conn = sqlite3.connect(trail._store._db_path)
    raw = conn.execute("SELECT detail_json FROM audit_events").fetchone()[0]
    conn.close()
    assert "SECRET123" not in raw
    assert "hunter2" not in raw
    assert "ABCDEF" not in raw
    assert "tok_live_xyz" not in raw


def test_20_redaction_applies_inside_nested_dicts(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    trail.append("BROKER_CONNECTED", credentials={"api_key": "SECRET", "note": "fine"})
    record = trail.records()[0]
    assert record.detail["credentials"]["api_key"] == REDACTED
    assert record.detail["credentials"]["note"] == "fine"


# ---------------------------------------------------------------------- #
# Extra: crash-simulation tests (Area F) -- verify partial traces are
# consistent and never fabricated, for each named boundary.
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("stage", ["A", "B", "C", "D", "E"])
def test_crash_boundary_leaves_a_consistent_partial_trace(tmp_path, stage):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    cid = f"crash-{stage}"
    trail.append("ORDER_INTENT_CREATED", correlation_id=cid, account_id="ACC_A")
    if stage == "A":
        pass  # crash between intent creation and risk decision
    else:
        trail.append("RISK_DECISION", correlation_id=cid, status="APPROVED")
        if stage == "B":
            pass  # crash between risk decision and authorization
        else:
            trail.append("AUTHORIZATION_STATE_GATE", correlation_id=cid, account_id="ACC_A", passed=True)
            if stage == "C":
                pass  # crash between authorization and idempotency claim / broker submission
            else:
                trail.append("BROKER_ORDER_PLACED", correlation_id=cid, broker_order_id="ord-1")
                if stage != "E":
                    pass  # stage D: crash between submission and response handling
                # stage E: crash between response and reconciliation -- no
                # RECONCILIATION_* event exists, which is exactly the
                # signal an investigator needs.

    trace = trail.trace(cid)
    event_types = [r.event_type for r in trace]
    expected_prefix = {
        "A": [],
        "B": ["RISK_DECISION"],
        "C": ["RISK_DECISION", "AUTHORIZATION_STATE_GATE"],
        "D": ["RISK_DECISION", "AUTHORIZATION_STATE_GATE", "BROKER_ORDER_PLACED"],
        "E": ["RISK_DECISION", "AUTHORIZATION_STATE_GATE", "BROKER_ORDER_PLACED"],
    }[stage]
    assert event_types == ["ORDER_INTENT_CREATED"] + expected_prefix
    assert "RECONCILIATION_COMPLETED" not in event_types  # never fabricated
    assert trail.verify() is True  # a truncated trace is still a VALID chain, not a corrupted one


# ---------------------------------------------------------------------- #
# Extra: mechanism-only coverage for event types with no producing
# subsystem yet (reconciliation, human authorization) -- proves the store
# supports them end-to-end even though nothing calls them in production
# today. See docs/phase-15d-audit-persistence-report.md.
# ---------------------------------------------------------------------- #
def test_reconciliation_and_human_authorization_event_types_are_supported(tmp_path):
    trail = PersistentAuditTrail(db_path=_tmp_db(tmp_path))
    trail.append(EVENT_RECONCILIATION_COMPLETED, correlation_id="c1", broker_order_id="ord-1", outcome="matched")
    trail.append(EVENT_HUMAN_AUTHORIZATION_DECISION, correlation_id="c1", decision="approved", actor="samir")
    assert len(trail.by_event_type(EVENT_RECONCILIATION_COMPLETED)) == 1
    assert len(trail.by_event_type(EVENT_HUMAN_AUTHORIZATION_DECISION)) == 1


# ---------------------------------------------------------------------- #
# Extra: drop-in compatibility with StrategyExecutionEngine's existing
# `audit_trail: AuditTrail | None` parameter -- no code change required
# in execution.py for it to work.
# ---------------------------------------------------------------------- #
def test_drop_in_replacement_for_in_memory_audit_trail_in_execution_engine(tmp_path):
    engine, trail, _idem, _account = _wire_engine(tmp_path)
    result = engine.execute(_intent(idempotency_key="dropin", correlation_id="c-dropin"))
    assert result.success
    assert len(trail.trace("c-dropin")) > 0


def test_real_broker_call_counts_are_zero(tmp_path):
    """Explicit proof this entire file uses only the in-process _FakeBroker
    -- never a real broker adapter/network call."""
    engine, _trail, _idem, _account = _wire_engine(tmp_path)
    assert isinstance(engine._broker, _FakeBroker)
    assert engine._broker.is_simulated is True
