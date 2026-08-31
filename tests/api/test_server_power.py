"""EC2 power control routes: POST /api/servers/{id}/{start,stop,restart}.

Every AWS interaction is mocked -- these tests never invoke a real
Lambda, never touch EC2. They assert the route's contract: RBAC, the
orchestrator action it calls, DB status write, audit row, and that the
orchestrator's safe-stop block surfaces as a 409 (never bypassed).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trading.api.lambda_client import LambdaInvokeError
from trading.database import models


@pytest.fixture
def server(seed_server):
    return seed_server(name="strategy-01", ec2_instance_id="i-abc", status="STOPPED", os="linux")


def test_requires_auth(client, server):
    r = client.post("/api/servers/strategy-01/start")
    assert r.status_code == 401


def test_viewer_forbidden(client, viewer_auth, server):
    r = client.post("/api/servers/strategy-01/start", headers=viewer_auth)
    assert r.status_code == 403


def test_trader_forbidden_needs_trading_control(client, trader_auth, server):
    # trader has START/STOP/RESTART for algos but NOT TRADING_CONTROL --
    # server power is deliberately gated higher.
    r = client.post("/api/servers/strategy-01/stop", headers=trader_auth)
    assert r.status_code == 403


def test_unknown_server_404(client, auth):
    r = client.post("/api/servers/nope/start", headers=auth)
    assert r.status_code == 404


def test_start_happy_path(client, auth, server, db_session):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {"success": True, "server_id": "i-abc", "status": "STARTING"}
        r = client.post("/api/servers/strategy-01/start", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["status"] == "STARTING"
    assert body["ec2_instance_id"] == "i-abc"
    assert mock_invoke.call_args.args[0] == "start_ec2"
    assert mock_invoke.call_args.kwargs["instance_id"] == "i-abc"
    assert mock_invoke.call_args.kwargs["force"] is False

    db_session.expire_all()
    assert db_session.query(models.Server).filter_by(name="strategy-01").one().status == "STARTING"
    audit_row = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "SERVER_START")
        .order_by(models.AuditLog.id.desc())
        .first()
    )
    assert audit_row is not None
    assert audit_row.target == "server:strategy-01"


def test_stop_forwards_force_flag(client, auth, server):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {"success": True, "server_id": "i-abc", "status": "STOPPING"}
        r = client.post("/api/servers/strategy-01/stop?force=true", headers=auth)
    assert r.status_code == 200
    assert mock_invoke.call_args.args[0] == "stop_ec2"
    assert mock_invoke.call_args.kwargs["force"] is True


def test_safe_stop_block_is_409(client, auth, server):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {
            "success": False,
            "safe_stop": "blocked",
            "error": "2 trading process(es) still alive on i-abc",
            "running": ["python main.py"],
        }
        r = client.post("/api/servers/strategy-01/stop", headers=auth)
    assert r.status_code == 409
    assert "still alive" in r.json()["detail"]


def test_restart_running_instance(client, auth, seed_server):
    seed_server(name="running-01", ec2_instance_id="i-run", status="RUNNING", os="linux")
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {"success": True, "server_id": "i-run", "status": "REBOOTING"}
        r = client.post("/api/servers/running-01/restart", headers=auth)
    assert r.status_code == 200
    assert r.json()["status"] == "REBOOTING"
    assert mock_invoke.call_args.args[0] == "restart_ec2"


def test_lambda_unreachable_is_502(client, auth, server):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.side_effect = LambdaInvokeError("no route to host")
        r = client.post("/api/servers/strategy-01/start", headers=auth)
    assert r.status_code == 502


def test_orchestrator_generic_failure_is_502(client, auth, server):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {"success": False, "error": "start_instances failed: quota"}
        r = client.post("/api/servers/strategy-01/start", headers=auth)
    assert r.status_code == 502
    assert "quota" in r.json()["detail"]
