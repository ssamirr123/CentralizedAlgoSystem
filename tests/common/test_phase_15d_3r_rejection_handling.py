"""Phase 15D.3-R: clean broker-rejection idempotency handling.

Fixes the gap found via the real Attempt 2 AG7002 rejection (see
docs/phase-15d-2-live-canary-report.md and
docs/phase-15d-3-post-canary-verification-report.md): a broker call that
returns a definitive, non-exceptional REJECTED OrderResult (no order_id)
and then exhausts retries used to leave the idempotency record at
STATUS_PENDING forever, indistinguishable from a genuinely unresolved
attempt. `ConfirmedRejectionError` (trading/common/execution.py) closes
this gap for the one case it is actually safe to close: a broker response
that unambiguously says "rejected, nothing created".

Every test here uses an in-process fake broker only -- zero real broker
calls, zero real orders.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trading.common.audit_store import PersistentAuditTrail
from trading.common.broker import BrokerClient, OrderResult, OrderSide, OrderType, Quote
from trading.common.broker_manager import BrokerManager
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import (
    STATUS_AMBIGUOUS,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_REJECTED,
    AmbiguousIdempotencyStateError,
    SqliteIdempotencyStore,
)
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount


def _intent(**kwargs) -> OrderIntent:
    defaults = dict(
        strategy_id="StratA", account_id="ACC_A", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=50, order_type=OrderType.MARKET, idempotency_key="k1",
        correlation_id="corr-1",
    )
    defaults.update(kwargs)
    return OrderIntent(**defaults)


class _ScriptedBroker(BrokerClient):
    """A fake broker whose place_order() behavior is scripted per-call --
    never touches a network, never a real adapter."""

    is_simulated = True

    def __init__(self, responses) -> None:
        """`responses`: a list of either an OrderResult, or an Exception
        instance to raise, consumed one per place_order() call (repeats
        the last entry if more calls happen than entries provided)."""
        self._responses = list(responses)
        self._call_count = 0
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
        idx = min(self._call_count, len(self._responses) - 1)
        response = self._responses[idx]
        self._call_count += 1
        if isinstance(response, Exception):
            raise response
        return response

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self):
        return []


def _engine(broker, tmp_path: Path, *, max_retries=1):
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = TradingAccount(account_id="ACC_A", account_name="A", broker_id="fake", execution_mode=ExecutionMode.PAPER)
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("StratA", "ACC_A")
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=max_retries, retry_delay_seconds=0, allow_market_emergency=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store,
    )
    return engine, idem_store, audit_trail


# ---------------------------------------------------------------------- #
# Case A -- explicit broker rejection, broker order NOT_FOUND (i.e. no
# order_id was ever issued) -> idempotency reaches terminal REJECTED.
# ---------------------------------------------------------------------- #
def test_case_a_confirmed_rejection_reaches_terminal_rejected_status(tmp_path):
    broker = _ScriptedBroker([
        OrderResult(order_id="", symbol="NIFTY", side=OrderSide.BUY, quantity=50, status="REJECTED", message="AG7002: IP not registered"),
    ])
    engine, idem_store, audit_trail = _engine(broker, tmp_path, max_retries=1)
    result = engine.execute(_intent(idempotency_key="case-a-key"))

    assert not result.success
    assert "rejected by broker" in result.message.lower()

    record = idem_store.get("case-a-key")
    assert record is not None
    assert record.status == STATUS_REJECTED
    assert record.broker_order_id == ""

    events = audit_trail.by_event_type("CONFIRMED_REJECTION")
    assert len(events) == 1
    assert events[0].idempotency_key == "case-a-key"


def test_case_a_confirmed_rejection_is_still_safely_retried_before_exhaustion(tmp_path):
    """Phase 15D-DR's own guarantee (confirmed rejection retried with a
    new price/attempt) is unaffected: with 2 retries and the SECOND
    attempt succeeding, execution completes normally -- ConfirmedRejectionError
    on attempt 1 does not immediately abort like AmbiguousOrderStateError does."""
    broker = _ScriptedBroker([
        OrderResult(order_id="", symbol="NIFTY", side=OrderSide.BUY, quantity=50, status="REJECTED", message="transient"),
        OrderResult(order_id="ORD-2", symbol="NIFTY", side=OrderSide.BUY, quantity=50, status="FILLED", message="ok"),
    ])
    engine, idem_store, _audit = _engine(broker, tmp_path, max_retries=2)
    result = engine.execute(_intent(idempotency_key="case-a-retry-key"))
    assert result.success
    assert result.order_id == "ORD-2"
    record = idem_store.get("case-a-retry-key")
    assert record.status == STATUS_COMPLETED


# ---------------------------------------------------------------------- #
# Case B -- REJECTED status but a real order_id is ALSO present: this
# codebase's own AngelOneBroker never produces this shape, but the engine
# must not blindly trust it if some adapter someday does.
# ---------------------------------------------------------------------- #
def test_case_b_rejected_with_order_id_is_treated_as_ambiguous_not_rejected(tmp_path):
    broker = _ScriptedBroker([
        OrderResult(order_id="SUSPICIOUS-1", symbol="NIFTY", side=OrderSide.BUY, quantity=50, status="REJECTED", message="inconsistent"),
    ])
    engine, idem_store, audit_trail = _engine(broker, tmp_path, max_retries=1)
    result = engine.execute(_intent(idempotency_key="case-b-key"))

    assert not result.success
    assert "ambiguous" in result.message.lower()

    record = idem_store.get("case-b-key")
    assert record.status == STATUS_AMBIGUOUS  # NOT blindly marked REJECTED
    events = audit_trail.by_event_type("AMBIGUOUS_ORDER_STATE")
    assert len(events) == 1


# ---------------------------------------------------------------------- #
# Case C -- ambiguous broker response (raised exception): no retry, no
# replacement, no second order, reconciliation required.
# ---------------------------------------------------------------------- #
def test_case_c_ambiguous_response_no_retry_no_second_order(tmp_path):
    call_count = {"n": 0}

    class _CountingBroker(_ScriptedBroker):
        def place_order(self, *a, **k):
            call_count["n"] += 1
            raise ConnectionError("simulated network failure")

    broker = _CountingBroker([])
    engine, idem_store, audit_trail = _engine(broker, tmp_path, max_retries=3)  # even with retries configured
    result = engine.execute(_intent(idempotency_key="case-c-key"))

    assert not result.success
    assert "ambiguous" in result.message.lower()
    assert call_count["n"] == 1  # never retried despite max_retries=3

    record = idem_store.get("case-c-key")
    assert record.status == STATUS_AMBIGUOUS

    # A second execute() attempt for the SAME key must not reach the broker either.
    with pytest.raises(AmbiguousIdempotencyStateError):
        engine.execute(_intent(idempotency_key="case-c-key"))
    assert call_count["n"] == 1


# ---------------------------------------------------------------------- #
# Case D -- successful broker order -> COMPLETED, broker_order_id persisted.
# ---------------------------------------------------------------------- #
def test_case_d_successful_order_persists_completed(tmp_path):
    broker = _ScriptedBroker([
        OrderResult(order_id="ORD-OK", symbol="NIFTY", side=OrderSide.BUY, quantity=50, status="FILLED", message="ok"),
    ])
    engine, idem_store, _audit = _engine(broker, tmp_path, max_retries=1)
    result = engine.execute(_intent(idempotency_key="case-d-key"))

    assert result.success
    assert result.order_id == "ORD-OK"
    record = idem_store.get("case-d-key")
    assert record.status == STATUS_COMPLETED
    assert record.broker_order_id == "ORD-OK"


# ---------------------------------------------------------------------- #
# Case E -- replay after a clean rejection: the original rejected intent
# cannot accidentally become a new live order under the SAME key; a
# genuinely new, explicitly authorized intent (a NEW idempotency_key) can
# still reach the broker normally.
# ---------------------------------------------------------------------- #
def test_case_e_replay_after_clean_rejection_never_reaches_the_broker(tmp_path):
    call_count = {"n": 0}

    class _CountingRejectBroker(_ScriptedBroker):
        def place_order(self, *a, **k):
            call_count["n"] += 1
            return OrderResult(order_id="", symbol="NIFTY", side=OrderSide.BUY, quantity=50, status="REJECTED", message="AG7002")

    broker = _CountingRejectBroker([])
    engine, idem_store, _audit = _engine(broker, tmp_path, max_retries=1)

    first = engine.execute(_intent(idempotency_key="case-e-key"))
    assert not first.success
    assert call_count["n"] == 1
    assert idem_store.get("case-e-key").status == STATUS_REJECTED

    # Replay with the SAME key: must not call the broker again.
    second = engine.execute(_intent(idempotency_key="case-e-key"))
    assert not second.success
    assert call_count["n"] == 1  # unchanged -- broker was never called a second time
    assert "previously failed" in second.message.lower() or "status=rejected" in second.message.lower()


def test_case_e_a_genuinely_new_authorized_intent_uses_a_new_key_and_can_reach_the_broker(tmp_path):
    """A NEW idempotency_key represents a NEW, explicitly authorized
    intent (per this repo's own established convention -- idempotency_key
    is never auto-generated, see order_intent.py) -- it is not blocked by
    an unrelated prior rejection under a DIFFERENT key."""
    broker = _ScriptedBroker([
        OrderResult(order_id="ORD-NEW", symbol="NIFTY", side=OrderSide.BUY, quantity=50, status="FILLED", message="ok"),
    ])
    engine, idem_store, _audit = _engine(broker, tmp_path, max_retries=1)
    result = engine.execute(_intent(idempotency_key="case-e-new-key"))
    assert result.success
    assert result.order_id == "ORD-NEW"


# ---------------------------------------------------------------------- #
# Restart durability of the confirmed-rejection fix itself.
# ---------------------------------------------------------------------- #
def test_confirmed_rejection_status_survives_restart(tmp_path):
    broker = _ScriptedBroker([
        OrderResult(order_id="", symbol="NIFTY", side=OrderSide.BUY, quantity=50, status="REJECTED", message="AG7002"),
    ])
    engine, idem_store, _audit = _engine(broker, tmp_path, max_retries=1)
    engine.execute(_intent(idempotency_key="restart-key"))
    assert idem_store.get("restart-key").status == STATUS_REJECTED

    # Fresh store instance against the same file -- simulates a restart.
    idem_store2 = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    record = idem_store2.get("restart-key")
    assert record is not None
    assert record.status == STATUS_REJECTED
