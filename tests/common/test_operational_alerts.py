"""Phase 16.11: trading/common/operational_alerts.py -- OperationalAlertStore
in isolation. Proves deduplication (no repeated-poll spam), resolution,
bounded history, and optional audit-trail integration."""
from __future__ import annotations

from trading.common.operational_alerts import AlertSeverity, OperationalAlertStore


def _raise(store, code="WORKER_OFFLINE", source_id="w1"):
    return store.raise_alert(
        code=code, severity=AlertSeverity.CRITICAL, category="WORKER",
        source_type="worker", source_id=source_id, message="worker offline",
    )


def test_raise_alert_is_idempotent_no_duplicate_spam():
    store = OperationalAlertStore()
    first = _raise(store)
    for _ in range(10):
        again = _raise(store)
        assert again.alert_id == first.alert_id
    assert len(store.active_alerts()) == 1


def test_resolve_clears_the_active_alert():
    store = OperationalAlertStore()
    _raise(store)
    assert len(store.active_alerts()) == 1
    resolved = store.resolve(code="WORKER_OFFLINE", source_type="worker", source_id="w1")
    assert resolved is not None
    assert resolved.active is False
    assert resolved.resolved_at != ""
    assert store.active_alerts() == []


def test_resolve_with_nothing_active_is_a_safe_no_op():
    store = OperationalAlertStore()
    assert store.resolve(code="WORKER_OFFLINE", source_type="worker", source_id="w1") is None


def test_re_raise_after_resolve_creates_a_new_alert():
    store = OperationalAlertStore()
    first = _raise(store)
    store.resolve(code="WORKER_OFFLINE", source_type="worker", source_id="w1")
    second = _raise(store)
    assert second.alert_id != first.alert_id
    assert len(store.active_alerts()) == 1


def test_distinct_identities_do_not_collide():
    store = OperationalAlertStore()
    _raise(store, source_id="w1")
    _raise(store, source_id="w2")
    _raise(store, code="MARKET_DATA_STALE", source_id="w1")
    assert len(store.active_alerts()) == 3


def test_all_alerts_is_bounded_newest_first():
    store = OperationalAlertStore()
    for i in range(5):
        _raise(store, source_id=f"w{i}")
    history = store.all_alerts(limit=3)
    assert len(history) == 3
    assert history[0].source_id == "w4"  # newest first


def test_audit_trail_receives_raise_and_resolve_events():
    events = []

    class _FakeAuditTrail:
        def append(self, event_type, **detail):
            events.append((event_type, detail))

    store = OperationalAlertStore(audit_trail=_FakeAuditTrail())
    _raise(store)
    store.resolve(code="WORKER_OFFLINE", source_type="worker", source_id="w1")

    event_types = [e[0] for e in events]
    assert "ALERT_RAISED_WORKER_OFFLINE" in event_types
    assert "ALERT_RESOLVED_WORKER_OFFLINE" in event_types


def test_no_audit_trail_is_a_safe_default():
    store = OperationalAlertStore()  # audit_trail=None
    _raise(store)  # must not raise
    store.resolve(code="WORKER_OFFLINE", source_type="worker", source_id="w1")
