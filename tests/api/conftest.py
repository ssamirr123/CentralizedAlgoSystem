"""API-test helpers: seed a server / algo row directly in the DB, and
create authenticated principals (Stage 18)."""
from __future__ import annotations

import pytest

from trading.api.security.passwords import hash_password
from trading.database import models


# --------------------------------------------------------------------------
# Auth fixtures
# --------------------------------------------------------------------------
_TEST_PW = "Testpass-123!"


@pytest.fixture
def make_user(db_session):
    def _make(username="op", role="operator", password=_TEST_PW, **kw):
        u = models.User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            is_active=kw.pop("is_active", True),
            **kw,
        )
        db_session.add(u)
        db_session.commit()
        db_session.refresh(u)
        return u

    return _make


@pytest.fixture
def token_for(client):
    def _tok(username, password=_TEST_PW):
        r = client.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    return _tok


@pytest.fixture
def bearer(make_user, token_for):
    """Header factory: bearer(role="viewer") -> {"Authorization": "Bearer ..."}."""
    made: dict[str, str] = {}

    def _hdr(role="operator", username=None):
        username = username or role
        if username not in made:
            make_user(username=username, role=role)
            made[username] = token_for(username)
        return {"Authorization": f"Bearer {made[username]}"}

    return _hdr


@pytest.fixture
def auth(bearer):
    """Back-compat name used across the existing API tests. Now an
    operator Bearer token (VIEW + START/STOP/RESTART + TRADING_CONTROL) so
    every pre-Stage-18 control/read test keeps passing unchanged."""
    return bearer(role="operator")


@pytest.fixture
def admin_auth(bearer):
    return bearer(role="admin")


@pytest.fixture
def viewer_auth(bearer):
    return bearer(role="viewer")


@pytest.fixture
def trader_auth(bearer):
    return bearer(role="trader")


@pytest.fixture
def service_auth(api_key):
    return {"X-API-Key": api_key}


@pytest.fixture
def seed_server(db_session):
    def _make(name="ec2-1", ec2_instance_id="i-test", region="ap-south-1", status="RUNNING", **kw):
        srv = models.Server(
            name=name, ec2_instance_id=ec2_instance_id, region=region, status=status, **kw
        )
        db_session.add(srv)
        db_session.commit()
        db_session.refresh(srv)
        return srv

    return _make


@pytest.fixture
def seed_algo(db_session):
    def _make(server, name="example_strategy", status="STOPPED", enabled=True, script_path=None):
        algo = models.Algo(
            name=name,
            server_id=server.id,
            script_path=script_path or f"trading/algos/{name}/main.py",
            status=status,
            enabled=enabled,
        )
        db_session.add(algo)
        db_session.commit()
        db_session.refresh(algo)
        return algo

    return _make
