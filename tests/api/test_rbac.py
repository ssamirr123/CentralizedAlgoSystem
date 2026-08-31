"""Stage 18 -- RBAC: the six permissions (VIEW/START/STOP/RESTART/
TRADING_CONTROL/ADMIN), role bundles, the machine service identity, and
the audit trail for denials and control actions."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trading.database import models


@pytest.fixture
def env(seed_server, seed_algo):
    server = seed_server(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="RUNNING")
    seed_algo(server, name="example_strategy")
    return server


def _start(client, headers):
    with patch("trading.api.routes.invoke_orchestrator",
               return_value={"success": True, "job_id": "j", "status": "STARTING"}):
        return client.post("/api/algo/start",
                           json={"algo_id": "example_strategy", "server_id": "ec2-1"}, headers=headers)


def _update(client, headers):
    with patch("trading.api.routes.invoke_orchestrator",
               return_value={"success": True, "job_id": "j", "status": "UPDATING"}):
        return client.post("/api/algo/update",
                           json={"algo_id": "example_strategy", "server_id": "ec2-1"}, headers=headers)


# --- viewer -------------------------------------------------------------
def test_viewer_can_read_not_control(client, viewer_auth, env):
    assert client.get("/api/algos", headers=viewer_auth).status_code == 200
    assert _start(client, viewer_auth).status_code == 403
    assert _update(client, viewer_auth).status_code == 403
    assert client.get("/api/admin/users", headers=viewer_auth).status_code == 403


# --- trader -------------------------------------------------------------
def test_trader_can_start_stop_restart_not_trading_control(client, trader_auth, env):
    assert _start(client, trader_auth).status_code == 200
    assert _update(client, trader_auth).status_code == 403           # UPDATE needs TRADING_CONTROL
    assert client.post("/api/algos", headers=trader_auth,
                       json={"algo_id": "x", "server_id": "ec2-1"}).status_code == 403  # register needs TRADING_CONTROL
    assert client.get("/api/admin/audit", headers=trader_auth).status_code == 403


# --- operator ---------------------------------------------------------
def test_operator_has_trading_control_not_admin(client, bearer, env):
    op = bearer(role="operator")
    assert _start(client, op).status_code == 200
    assert _update(client, op).status_code == 200
    assert client.get("/api/admin/users", headers=op).status_code == 403


# --- admin ----------------------------------------------------------
def test_admin_has_everything(client, admin_auth, env):
    assert _start(client, admin_auth).status_code == 200
    assert _update(client, admin_auth).status_code == 200
    assert client.get("/api/admin/users", headers=admin_auth).status_code == 200


# --- machine service identity --------------------------------------
def test_service_key_can_read_and_ingest_but_not_control(client, service_auth, env):
    assert client.get("/api/algos", headers=service_auth).status_code == 200
    hb = client.post("/api/heartbeat", headers=service_auth,
                     json={"algo_id": "example_strategy", "server_id": "ec2-1", "status": "RUNNING"})
    assert hb.status_code == 200
    assert _start(client, service_auth).status_code == 403
    assert _update(client, service_auth).status_code == 403
    assert client.get("/api/admin/users", headers=service_auth).status_code == 403


def test_user_without_ingest_cannot_post_heartbeat(client, viewer_auth, env):
    r = client.post("/api/heartbeat", headers=viewer_auth,
                    json={"algo_id": "example_strategy", "server_id": "ec2-1", "status": "RUNNING"})
    assert r.status_code == 403


# --- audit trail ---------------------------------------------------
def test_control_action_and_denial_are_audited(client, viewer_auth, trader_auth, env, db_session):
    _start(client, trader_auth)          # success
    _start(client, viewer_auth)          # denied
    actions = {r.action: r.outcome for r in db_session.query(models.AuditLog).all()}
    assert actions.get("ALGO_START") == "success"
    assert actions.get("PERMISSION_DENIED") == "denied"


def test_new_user_defaults_to_viewer(client, admin_auth):
    r = client.post("/api/admin/users", headers=admin_auth,
                    json={"username": "newbie", "password": "Fresh-Pass-99!"})
    assert r.status_code == 201
    assert r.json()["role"] == "viewer"
    assert r.json()["effective_permissions"] == ["VIEW"]


def test_cannot_demote_last_admin(client, admin_auth, db_session):
    admin = db_session.query(models.User).filter(models.User.role == "admin").one()
    r = client.patch(f"/api/admin/users/{admin.id}", headers=admin_auth, json={"role": "viewer"})
    assert r.status_code == 409


def test_reset_password_forces_change_and_revokes_sessions(client, admin_auth, bearer, token_for, db_session):
    bearer(role="trader", username="bob")
    bob = db_session.query(models.User).filter(models.User.username == "bob").one()
    r = client.post(f"/api/admin/users/{bob.id}/reset-password", headers=admin_auth,
                    json={"new_password": "Rot4ted-Pass!!"})
    assert r.status_code == 204
    live = db_session.query(models.AuthSession).filter(
        models.AuthSession.user_id == bob.id, models.AuthSession.revoked_at.is_(None)
    ).count()
    assert live == 0
