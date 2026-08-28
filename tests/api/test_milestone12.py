"""Rate limiting, live server health, heartbeat alerting.
Ported from trading/api/test_milestone12_local.py."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trading.api import deps as api_deps
from trading.api.lambda_client import LambdaInvokeError
from trading.database import models


@pytest.fixture
def server(seed_server):
    return seed_server(name="ec2-1", ec2_instance_id="i-test", status="STOPPED")


# --------------------------- live server health ---------------------------
def test_live_health_populates_and_persists(client, auth, server, db_session):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {
            "success": True, "server_id": "i-test", "ec2_status": "RUNNING",
            "ssm_status": "ONLINE", "last_ping": "2026-08-13T00:00:00Z", "healthy": True,
        }
        r = client.get("/api/server/status", params={"server_id": "ec2-1", "live": "true"}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "RUNNING"
        assert body["ssm_status"] == "ONLINE"
        assert body["live_check_healthy"] is True
        assert mock_invoke.call_args.args[0] == "check_ec2_health"

    db_session.expire_all()
    assert db_session.query(models.Server).filter(models.Server.name == "ec2-1").one().status == "RUNNING"


def test_live_omitted_no_lambda_call(client, auth, server):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        r = client.get("/api/server/status", params={"server_id": "ec2-1"}, headers=auth)
        assert not mock_invoke.called
        assert r.json()["ssm_status"] is None


def test_live_health_lambda_failure_degrades(client, auth, server):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.side_effect = LambdaInvokeError("lambda unreachable")
        r = client.get("/api/server/status", params={"server_id": "ec2-1", "live": "true"}, headers=auth)
    assert r.status_code == 200  # not a 500
    assert r.json()["ssm_status"] is None


# --------------------------- heartbeat alerting ---------------------------
def test_unregistered_algo_heartbeat_no_alert(client, auth, server):
    with patch("trading.api.routes.alert_service") as mock_alerts:
        r = client.post(
            "/api/heartbeat",
            json={"algo_id": "unregistered_algo", "server_id": "ec2-1", "status": "RUNNING"},
            headers=auth,
        )
        assert r.status_code == 404
        assert not mock_alerts.method_calls


def test_heartbeat_status_transition_alerts(client, auth, server):
    assert client.post("/api/algos", json={"algo_id": "new_algo", "server_id": "ec2-1"}, headers=auth).status_code == 201

    def hb(status):
        return client.post(
            "/api/heartbeat",
            json={"algo_id": "new_algo", "server_id": "ec2-1", "status": status},
            headers=auth,
        )

    with patch("trading.api.routes.alert_service") as a:
        assert hb("RUNNING").status_code == 200  # STOPPED default -> RUNNING
        assert a.strategy_recovered.called
        assert not a.strategy_crashed.called and not a.strategy_stopped.called

    with patch("trading.api.routes.alert_service") as a:
        hb("RUNNING")  # no change
        assert not a.strategy_recovered.called

    with patch("trading.api.routes.alert_service") as a:
        hb("ERROR")
        assert a.strategy_crashed.called

    with patch("trading.api.routes.alert_service") as a:
        hb("RUNNING")
        assert a.strategy_recovered.called

    with patch("trading.api.routes.alert_service") as a:
        hb("STOPPED")
        assert a.strategy_stopped.called


def test_first_heartbeat_error_reason(client, auth, server):
    assert client.post("/api/algos", json={"algo_id": "a2", "server_id": "ec2-1"}, headers=auth).status_code == 201
    with patch("trading.api.routes.alert_service") as a:
        client.post("/api/heartbeat", json={"algo_id": "a2", "server_id": "ec2-1", "status": "ERROR"}, headers=auth)
        assert a.strategy_crashed.called
        assert "Status changed" in a.strategy_crashed.call_args.kwargs.get("reason", "")


# --------------------------- rate limiting ---------------------------
def test_rate_limit_blocks_after_max(client, auth, server, db_session):
    db_session.query(models.RateLimitWindow).delete()
    db_session.commit()

    original = api_deps.RATE_LIMIT_MAX_REQUESTS
    api_deps.RATE_LIMIT_MAX_REQUESTS = 5
    try:
        statuses = [client.get("/api/servers", headers=auth).status_code for _ in range(6)]
        assert statuses == [200, 200, 200, 200, 200, 429]

        r = client.get("/api/servers", headers=auth)
        assert "retry-after" in {k.lower() for k in r.headers}

        # invalid key -> 401 before the limiter (auth runs first)
        assert client.get("/api/servers", headers={"X-API-Key": "wrong"}).status_code == 401
    finally:
        api_deps.RATE_LIMIT_MAX_REQUESTS = original
