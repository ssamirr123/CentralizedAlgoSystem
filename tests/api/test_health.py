"""GET /api/health -- ported from trading/api/test_health_local.py."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

_KEYS = {"status", "service", "timestamp", "database"}


def test_health_connected(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == _KEYS
    assert body["status"] == "ok"
    assert body["service"] == "centralized-algo-backend"
    assert body["database"] == "connected"
    datetime.fromisoformat(body["timestamp"].replace("Z", "+00:00"))  # parses


def test_health_no_auth_required(client):
    assert client.get("/api/health").status_code not in (401, 403)


def test_health_database_failure(client):
    def _boom(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("simulated down"))

    with patch("trading.api.health.engine") as fake_engine:
        fake_engine.connect.side_effect = _boom
        r = client.get("/api/health")

    assert r.status_code == 503  # controlled, not a 500
    body = r.json()
    assert set(body) == _KEYS
    assert body["status"] == "degraded"
    assert body["database"].startswith("error")
    assert "sqlite" not in body["database"].lower() and "://" not in body["database"]
    assert body["service"] == "centralized-algo-backend"


def test_health_recovers_after_transient_failure(client):
    def _boom(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("down"))

    with patch("trading.api.health.engine") as fe:
        fe.connect.side_effect = _boom
        assert client.get("/api/health").status_code == 503

    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["database"] == "connected"


def test_legacy_health_still_distinct(client):
    legacy = client.get("/health").json()
    assert "timestamp_utc" in legacy and "database" not in legacy
    assert legacy["service"] == "central-strategy-monitor"
