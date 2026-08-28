"""Backward-compat contract for the legacy endpoints (/health,
/update_strategy, /strategies) after their move to trading/api/legacy.py.
Ported from trading/api/test_legacy_compat_local.py."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trading.database.models import StrategyHeartbeat

_OUT_KEYS = {
    "strategy_name", "server_name", "status", "current_mtm", "day_pnl",
    "number_of_trades", "last_update_time", "received_at",
}


def _hb(**over):
    body = {
        "strategy_name": "compat_s",
        "server_name": "compat_srv",
        "status": "RUNNING",
        "current_mtm": 100.5,
        "day_pnl": 250.75,
        "number_of_trades": 4,
        "last_update_time": "2026-08-28T10:00:00Z",
    }
    body.update(over)
    return body


@pytest.fixture
def alerts():
    with patch("trading.api.legacy.alert_service") as m:
        yield m


def test_health_shape(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "timestamp_utc", "service"}
    assert body["status"] == "ok"
    assert body["service"] == "central-strategy-monitor"


def test_health_needs_no_auth(client):
    assert client.get("/health").status_code == 200


def test_update_strategy_create(client, alerts, db_session):
    r = client.post("/update_strategy", json=_hb())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == _OUT_KEYS
    assert body["status"] == "RUNNING"
    assert alerts.strategy_started.called

    rows = db_session.query(StrategyHeartbeat).all()
    assert len(rows) == 1
    assert rows[0].strategy_name == "compat_s"
    assert rows[0].current_mtm == 100.5
    assert rows[0].number_of_trades == 4


def test_update_strategy_upsert_and_transitions(client, alerts, db_session):
    client.post("/update_strategy", json=_hb(status="RUNNING"))

    alerts.reset_mock()
    r = client.post("/update_strategy", json=_hb(status="STOPPED", day_pnl=10.0))
    assert r.status_code == 200
    rows = db_session.query(StrategyHeartbeat).all()
    assert len(rows) == 1  # upsert, not insert
    assert rows[0].status == "STOPPED"
    assert alerts.strategy_stopped.called

    alerts.reset_mock()
    client.post("/update_strategy", json=_hb(status="RUNNING"))
    assert alerts.strategy_recovered.called

    alerts.reset_mock()
    client.post("/update_strategy", json=_hb(status="ERROR"))
    assert alerts.strategy_crashed.called


def test_update_strategy_day_loss_alert(client, alerts):
    alerts.reset_mock()
    client.post("/update_strategy", json=_hb(status="RUNNING", day_pnl=-25000.0))
    assert alerts.day_loss_exceeded.called
    kw = alerts.day_loss_exceeded.call_args.kwargs
    assert kw["loss"] == -25000.0 and kw["limit"] == 10000.0


@pytest.mark.parametrize("payload, why", [
    (_hb(number_of_trades=-1), "negative trades"),
    (_hb(status="BOGUS"), "invalid status enum"),
    ({"strategy_name": "x"}, "missing required fields"),
])
def test_update_strategy_validation_422(client, payload, why):
    assert client.post("/update_strategy", json=payload).status_code == 422, why


def test_strategies_list(client):
    client.post("/update_strategy", json=_hb(strategy_name="compat_s", status="RUNNING"))
    client.post("/update_strategy", json=_hb(strategy_name="compat_s2", status="RUNNING"))
    r = client.get("/strategies")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert all(set(item) == _OUT_KEYS for item in data)
    assert data[0]["received_at"] >= data[1]["received_at"]  # desc order


def test_strategies_needs_no_auth(client):
    assert client.get("/strategies").status_code == 200
