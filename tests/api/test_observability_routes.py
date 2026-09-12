"""Phase 13: observability API -- GET /api/observability/metrics|alerts|
trace/{id}|audit|audit/integrity. All read-only (VIEW); the events they
report are produced by actions already exercised in
tests/api/test_execution_routes.py (strategy start/stop, kill-switch
engage/disengage)."""
from __future__ import annotations


def test_unauthenticated_request_is_401(client):
    assert client.get("/api/observability/metrics").status_code == 401


def test_metrics_reflect_a_strategy_start(client, trader_auth):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)

    r = client.get("/api/observability/metrics", headers=trader_auth)
    assert r.status_code == 200
    body = r.json()
    assert "strategy_heartbeat:CombinedVwapNifty" in body["gauges"]


def test_alerts_list_reflects_a_strategy_stop(client, trader_auth):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    client.post("/api/strategies/CombinedVwapNifty/stop", headers=trader_auth)

    r = client.get("/api/observability/alerts", headers=trader_auth)
    assert r.status_code == 200
    alert_types = [a["alert_type"] for a in r.json()]
    assert "STRATEGY_STOPPED" in alert_types


def test_alerts_can_be_filtered_by_type(client, trader_auth, admin_auth):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    client.post("/api/strategies/CombinedVwapNifty/stop", headers=trader_auth)
    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": True, "reason": "halt"})

    r = client.get("/api/observability/alerts", headers=trader_auth, params={"alert_type": "KILL_SWITCH"})
    assert r.status_code == 200
    assert all(a["alert_type"] == "KILL_SWITCH" for a in r.json())
    assert len(r.json()) == 1


def test_kill_switch_alert_carries_the_actor(client, admin_auth):
    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": True, "reason": "manual halt"})

    r = client.get("/api/observability/alerts", headers=admin_auth)
    kill_alerts = [a for a in r.json() if a["alert_type"] == "KILL_SWITCH"]
    assert len(kill_alerts) == 1
    assert kill_alerts[0]["detail"]["by"].startswith("user:")
    assert kill_alerts[0]["detail"]["reason"] == "manual halt"
    assert kill_alerts[0]["severity"] == "CRITICAL"


def test_audit_trail_lists_strategy_lifecycle_events(client, trader_auth):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    client.post("/api/strategies/CombinedVwapNifty/stop", headers=trader_auth)

    r = client.get("/api/observability/audit", headers=trader_auth)
    assert r.status_code == 200
    event_types = {row["event_type"] for row in r.json()}
    assert "STRATEGY_STARTED" in event_types
    assert "STRATEGY_STOPPED" in event_types


def test_audit_trail_is_newest_first(client, trader_auth):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    client.post("/api/strategies/CombinedVwapNifty/stop", headers=trader_auth)

    rows = client.get("/api/observability/audit", headers=trader_auth).json()
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs, reverse=True)


def test_trace_for_unknown_correlation_id_is_an_empty_list_not_an_error(client, viewer_auth):
    r = client.get("/api/observability/trace/NO-SUCH-ID", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json() == []


def test_audit_integrity_reports_verified_true(client, trader_auth):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)

    r = client.get("/api/observability/audit/integrity", headers=trader_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["verified"] is True
    assert body["record_count"] >= 1


def test_observability_endpoints_are_view_gated_not_admin(client, viewer_auth):
    """A plain viewer (VIEW only) can read every observability endpoint --
    these are read-only and carry no secret, matching every other GET in
    this router."""
    for path in (
        "/api/observability/metrics",
        "/api/observability/alerts",
        "/api/observability/audit",
        "/api/observability/audit/integrity",
        "/api/observability/trace/anything",
    ):
        assert client.get(path, headers=viewer_auth).status_code == 200
