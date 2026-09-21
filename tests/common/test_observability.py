"""Phase 13: MetricsRegistry + AuditTrail (trading/common/observability.py).

No broker, no strategy, no execution engine here -- this file tests the
observability primitives in isolation, exactly as risk_manager.py/
strategy_registry.py's own test files test their primitives standalone.
"""
from __future__ import annotations

import pytest

from trading.common.observability import (
    ORDER_LIFECYCLE_STAGES,
    AuditTrail,
    MetricsRegistry,
    TamperDetectedError,
)


# --------------------------------------------------------------------------- #
# MetricsRegistry
# --------------------------------------------------------------------------- #
def test_counters_increment_per_entity_and_aggregate():
    m = MetricsRegistry()
    m.record_order_intent("StratA")
    m.record_order_intent("StratA")
    m.record_order_intent("StratB")

    snap = m.snapshot()
    assert snap.counters["order_intents:StratA"] == 2
    assert snap.counters["order_intents:StratB"] == 1
    assert snap.counters["order_intents:_all"] == 3


def test_all_named_counter_helpers():
    m = MetricsRegistry()
    m.record_order_approved("S")
    m.record_order_rejected("S", "reason")
    m.record_simulated_fill("S")
    m.record_real_fill("S")
    m.record_error("execution:S")

    snap = m.snapshot()
    assert snap.counters["orders_approved:S"] == 1
    assert snap.counters["orders_rejected:S"] == 1
    assert snap.counters["simulated_fills:S"] == 1
    assert snap.counters["real_fills:S"] == 1
    assert snap.counters["errors:execution:S"] == 1


def test_gauges_for_pnl_exposure_position():
    m = MetricsRegistry()
    m.record_pnl("S", 1234.5)
    m.record_exposure("S", 9000.0)
    m.record_position("S", "NIFTY", -65)

    snap = m.snapshot()
    assert snap.gauges["pnl:S"] == 1234.5
    assert snap.gauges["exposure:S"] == 9000.0
    assert snap.gauges["position:S:NIFTY"] == -65


def test_account_status_is_a_string_not_a_gauge():
    m = MetricsRegistry()
    m.record_account_status("ANGEL_MAIN", "CONNECTED")
    snap = m.snapshot()
    assert snap.statuses["account_status:ANGEL_MAIN"] == "CONNECTED"


def test_heartbeats_set_a_recent_timestamp_gauge():
    import time

    m = MetricsRegistry()
    before = time.time()
    m.record_strategy_heartbeat("S")
    m.record_broker_heartbeat("angelone")
    snap = m.snapshot()
    assert snap.gauges["strategy_heartbeat:S"] >= before
    assert snap.gauges["broker_heartbeat:angelone"] >= before


def test_execution_latency_recorded_per_entity_and_aggregate():
    m = MetricsRegistry()
    m.record_execution_latency("S", 0.12)
    m.record_execution_latency("S", 0.34)
    snap = m.snapshot()
    assert snap.latencies["execution_latency:S"] == [0.12, 0.34]
    assert snap.latencies["execution_latency:_all"] == [0.12, 0.34]


def test_latency_samples_are_bounded():
    m = MetricsRegistry(max_latency_samples=3)
    for i in range(10):
        m.observe_latency("x", float(i))
    snap = m.snapshot()
    assert len(snap.latencies["x"]) == 3
    assert snap.latencies["x"] == [7.0, 8.0, 9.0]


def test_snapshot_is_a_disconnected_copy():
    m = MetricsRegistry()
    m.record_order_intent("S")
    snap = m.snapshot()
    m.record_order_intent("S")
    assert snap.counters["order_intents:S"] == 1  # unaffected by the later increment


# --------------------------------------------------------------------------- #
# AuditTrail -- hash chain / tamper evidence
# --------------------------------------------------------------------------- #
def test_append_returns_a_linked_record():
    trail = AuditTrail()
    r1 = trail.append("EVENT_A", correlation_id="c1", strategy_id="S", foo="bar")
    r2 = trail.append("EVENT_B", correlation_id="c1", strategy_id="S")
    assert r1.prev_hash == AuditTrail.GENESIS_HASH
    assert r2.prev_hash == r1.hash
    assert r1.seq == 0 and r2.seq == 1
    assert r1.detail == {"foo": "bar"}


def test_trace_filters_by_correlation_id():
    trail = AuditTrail()
    trail.append("A", correlation_id="c1")
    trail.append("B", correlation_id="c2")
    trail.append("C", correlation_id="c1")

    trace = trail.trace("c1")
    assert [r.event_type for r in trace] == ["A", "C"]


def test_verify_passes_on_an_untouched_trail():
    trail = AuditTrail()
    for i in range(20):
        trail.append(f"EVENT_{i}", correlation_id="c", value=i)
    assert trail.verify() is True
    trail.verify_or_raise()  # must not raise


def test_verify_detects_a_modified_detail_field():
    trail = AuditTrail()
    trail.append("EVENT_A", correlation_id="c", amount=100)
    trail.append("EVENT_B", correlation_id="c")

    # Simulate "silent modification": mutate a past record's detail dict
    # in place (AuditRecord is a frozen dataclass, but `detail` is itself a
    # mutable dict object, so this is exactly the kind of direct in-memory
    # tamper the hash chain exists to catch).
    trail._records[0].detail["amount"] = 999999  # noqa: SLF001 - deliberate tamper for the test

    assert trail.verify() is False
    with pytest.raises(TamperDetectedError):
        trail.verify_or_raise()


def test_verify_detects_reordering():
    trail = AuditTrail()
    trail.append("FIRST", correlation_id="c")
    trail.append("SECOND", correlation_id="c")

    trail._records[0], trail._records[1] = trail._records[1], trail._records[0]  # noqa: SLF001

    assert trail.verify() is False


def test_verify_detects_a_deleted_record():
    trail = AuditTrail()
    trail.append("FIRST", correlation_id="c")
    trail.append("SECOND", correlation_id="c")
    trail.append("THIRD", correlation_id="c")

    del trail._records[1]  # noqa: SLF001 - deliberate tamper: silently remove a middle record

    assert trail.verify() is False


def test_records_returns_a_disconnected_copy():
    trail = AuditTrail()
    trail.append("A", correlation_id="c")
    records = trail.records()
    records.append("not a real record")  # type: ignore[arg-type]
    assert len(trail.records()) == 1  # unaffected


def test_order_lifecycle_stages_constant_is_in_pipeline_order():
    assert ORDER_LIFECYCLE_STAGES == (
        "ORDER_INTENT_CREATED", "RISK_DECISION", "EXECUTION_RESULT",
        "BROKER_ORDER_PLACED", "FILL", "POSITION_UPDATE", "PNL_UPDATE",
    )
