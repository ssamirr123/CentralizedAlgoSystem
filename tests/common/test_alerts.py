"""Phase 13: AlertManager (trading/common/alerts.py) -- the 7 named alert
types, in isolation from any strategy/broker/execution wiring (that
wiring is covered by tests/common/test_observability_wiring.py)."""
from __future__ import annotations

from trading.common.alerts import (
    ALERT_BROKER_DISCONNECTED,
    ALERT_DAILY_LOSS_LIMIT,
    ALERT_EXECUTION_ERROR,
    ALERT_KILL_SWITCH,
    ALERT_RISK_BREACH,
    ALERT_STRATEGY_STOPPED,
    ALERT_UNEXPECTED_POSITION,
    AlertManager,
)
from trading.common.observability import AuditTrail


def test_strategy_stopped_alert():
    mgr = AlertManager()
    alert = mgr.strategy_stopped("DoubleStraddelAlgo", reason="manual stop")
    assert alert.alert_type == ALERT_STRATEGY_STOPPED
    assert alert.severity == "WARNING"
    assert "DoubleStraddelAlgo" in alert.message
    assert mgr.alerts() == [alert]


def test_broker_disconnected_alert_is_critical():
    mgr = AlertManager()
    alert = mgr.broker_disconnected("angelone", reason="session expired")
    assert alert.alert_type == ALERT_BROKER_DISCONNECTED
    assert alert.severity == "CRITICAL"


def test_risk_breach_alert_carries_check_name_and_reason():
    mgr = AlertManager()
    alert = mgr.risk_breach("S", "MAX_ORDER_QUANTITY", "quantity 500 exceeds max 100", correlation_id="c1")
    assert alert.alert_type == ALERT_RISK_BREACH
    assert alert.correlation_id == "c1"
    assert alert.detail["check_name"] == "MAX_ORDER_QUANTITY"


def test_daily_loss_limit_alert_is_critical():
    mgr = AlertManager()
    alert = mgr.daily_loss_limit("ANGEL_MAIN", -12000.0, 10000.0)
    assert alert.alert_type == ALERT_DAILY_LOSS_LIMIT
    assert alert.severity == "CRITICAL"
    assert alert.detail["pnl"] == -12000.0


def test_unexpected_position_alert():
    mgr = AlertManager()
    alert = mgr.unexpected_position("S", "NIFTY", -65, expected=0)
    assert alert.alert_type == ALERT_UNEXPECTED_POSITION
    assert alert.detail["quantity"] == -65
    assert alert.detail["expected"] == 0


def test_execution_error_alert_is_critical():
    mgr = AlertManager()
    alert = mgr.execution_error("S", "order execution failed after retries", correlation_id="c2")
    assert alert.alert_type == ALERT_EXECUTION_ERROR
    assert alert.severity == "CRITICAL"
    assert alert.correlation_id == "c2"


def test_kill_switch_engaged_is_critical_disengaged_is_info():
    mgr = AlertManager()
    engaged = mgr.kill_switch(True, by="user:1", reason="manual halt")
    disengaged = mgr.kill_switch(False, by="user:1")
    assert engaged.alert_type == ALERT_KILL_SWITCH
    assert engaged.severity == "CRITICAL"
    assert disengaged.severity == "INFO"


def test_alerts_are_bounded():
    mgr = AlertManager(max_alerts=3)
    for i in range(10):
        mgr.strategy_stopped(f"S{i}")
    alerts = mgr.alerts()
    assert len(alerts) == 3
    assert [a.detail["strategy_id"] for a in alerts] == ["S7", "S8", "S9"]


def test_alerts_are_sequential():
    mgr = AlertManager()
    a1 = mgr.strategy_stopped("S1")
    a2 = mgr.strategy_stopped("S2")
    assert a2.seq == a1.seq + 1


# --------------------------------------------------------------------------- #
# Integration with AuditTrail: every alert is also durably recorded there
# --------------------------------------------------------------------------- #
def test_every_alert_is_appended_to_the_audit_trail_when_provided():
    trail = AuditTrail()
    mgr = AlertManager(audit_trail=trail)

    mgr.strategy_stopped("S")
    mgr.kill_switch(True, by="user:1", reason="halt")

    events = [r.event_type for r in trail.records()]
    assert events == ["ALERT_STRATEGY_STOPPED", "ALERT_KILL_SWITCH"]


def test_alerts_work_without_an_audit_trail():
    mgr = AlertManager()  # no audit_trail supplied
    alert = mgr.strategy_stopped("S")
    assert alert is not None  # must not raise
