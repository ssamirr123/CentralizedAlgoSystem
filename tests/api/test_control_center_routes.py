"""Control-center API routing / validation / response-shaping.
Ported from trading/api/test_routes_local.py.

The original could not run green because it never registered the algo it
tried to start (auto-create was disabled after that script was written).
Fixed here by seeding the algo row -- the assertions are unchanged.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trading.api.lambda_client import LambdaInvokeError
from trading.database import models


@pytest.fixture
def env(seed_server, seed_algo):
    server = seed_server(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="RUNNING")
    algo = seed_algo(server, name="example_strategy")
    return server, algo


def test_no_api_key_401(client):
    r = client.post("/api/algo/start", json={"algo_id": "example_strategy", "server_id": "ec2-1"})
    assert r.status_code == 401


def test_wrong_api_key_401(client):
    r = client.post(
        "/api/algo/start",
        json={"algo_id": "example_strategy", "server_id": "ec2-1"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert r.status_code == 401


def test_unknown_server_404(client, auth, env):
    r = client.post("/api/algo/start", json={"algo_id": "x", "server_id": "nonexistent"}, headers=auth)
    assert r.status_code == 404


def test_start_algo_happy_path(client, auth, env, db_session):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {
            "success": True, "job_id": "cmd-abc", "server_id": "i-test",
            "algo_id": "example_strategy", "status": "STARTING",
        }
        r = client.post(
            "/api/algo/start",
            json={"algo_id": "example_strategy", "server_id": "ec2-1", "requested_by": "test-user"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["job_id"] == "cmd-abc"
        assert body["status"] == "STARTING"
        assert body["command_id"] is not None
        assert mock_invoke.call_args.args[0] == "start_algo"
        assert mock_invoke.call_args.kwargs.get("algo_id") == "example_strategy"
        command_id = body["command_id"]

    row = db_session.query(models.Command).filter(models.Command.id == command_id).one()
    assert row.job_id == "cmd-abc"
    assert row.status == "STARTING"
    # Stage 18: requested_by is the authenticated identity, not the
    # client-supplied label -- so the audit trail can't be spoofed.
    assert row.requested_by == "operator"


def test_start_algo_lambda_failure_is_200_failed(client, auth, env):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.side_effect = LambdaInvokeError("boom")
        r = client.post(
            "/api/algo/start",
            json={"algo_id": "example_strategy", "server_id": "ec2-1"},
            headers=auth,
        )
    assert r.status_code == 200  # not a 500
    assert r.json()["status"] == "FAILED"


def test_get_command_status_progress_then_resolves(client, auth, env, db_session):
    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {
            "success": True, "job_id": "cmd-abc", "server_id": "i-test",
            "algo_id": "example_strategy", "status": "STARTING",
        }
        command_id = client.post(
            "/api/algo/start",
            json={"algo_id": "example_strategy", "server_id": "ec2-1"},
            headers=auth,
        ).json()["command_id"]

    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {"job_id": "cmd-abc", "ssm_status": "InProgress", "status": "IN_PROGRESS"}
        r = client.get(f"/api/command/{command_id}", headers=auth)
        assert r.status_code == 200
        assert r.json()["status"] == "STARTING"  # unchanged while in progress

    with patch("trading.api.routes.invoke_orchestrator") as mock_invoke:
        mock_invoke.return_value = {
            "success": True, "job_id": "cmd-abc", "ssm_status": "Success",
            "algo": "example_strategy", "status": "RUNNING", "pid": 4242,
        }
        r = client.get(f"/api/command/{command_id}", headers=auth)
        assert r.json()["status"] == "RUNNING"

    row = db_session.query(models.Command).filter(models.Command.id == command_id).one()
    assert row.status == "RUNNING"


def test_unknown_command_id_404(client, auth, env):
    assert client.get("/api/command/999999", headers=auth).status_code == 404


def test_server_status(client, auth, env):
    r = client.get("/api/server/status", params={"server_id": "ec2-1"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["ec2_instance_id"] == "i-test"


@pytest.mark.parametrize("path", ["/api/logs", "/api/pnl", "/api/positions"])
def test_readonly_collections_empty_but_wellformed(client, auth, env, path):
    r = client.get(path, params={"algo_id": "example_strategy", "server_id": "ec2-1"}, headers=auth)
    assert r.status_code == 200
    assert r.json() == []


def test_legacy_health_still_works_no_auth(client):
    assert client.get("/health").status_code == 200
