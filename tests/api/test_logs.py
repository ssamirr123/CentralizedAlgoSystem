"""POST/GET /api/logs -- ported from trading/api/test_logs_local.py."""
from __future__ import annotations

import pytest

_EVENTS = [
    {"algo_id": "algo1", "server_id": "ec2-1", "level": "INFO", "event": "ENTRY",
     "details": {"strike": 24750}, "timestamp": "2026-08-10T10:00:00+00:00"},
    {"algo_id": "algo1", "server_id": "ec2-1", "level": "WARNING", "event": "SL",
     "details": {"reason": "stop loss hit"}, "timestamp": "2026-08-10T10:15:00+00:00"},
    {"algo_id": "algo1", "server_id": "ec2-1", "level": "INFO", "event": "EXIT",
     "details": {}, "timestamp": "2026-08-11T09:00:00+00:00"},
]


@pytest.fixture
def ctx(client, auth, seed_server):
    seed_server(name="ec2-1", ec2_instance_id="i-test", status="RUNNING")
    assert client.post("/api/algos", json={"algo_id": "algo1", "server_id": "ec2-1"}, headers=auth).status_code == 201
    for e in _EVENTS:
        assert client.post("/api/logs", json=e, headers=auth).status_code == 200
    return client


def _get(c, auth, **params):
    return c.get("/api/logs", params={"algo_id": "algo1", "server_id": "ec2-1", **params}, headers=auth).json()


def test_post_log_no_auth_401(client, seed_server):
    seed_server(name="ec2-1")
    r = client.post("/api/logs", json={"algo_id": "x", "server_id": "ec2-1", "level": "INFO", "event": "ENTRY"})
    assert r.status_code == 401


def test_post_log_unknown_server_404(client, auth, seed_server):
    seed_server(name="ec2-1")
    r = client.post(
        "/api/logs",
        json={"algo_id": "x", "server_id": "nope", "level": "INFO", "event": "ENTRY"},
        headers=auth,
    )
    assert r.status_code == 404


def test_get_all_logs(ctx, auth):
    assert len(_get(ctx, auth)) == 3


def test_filter_by_level(ctx, auth):
    rows = _get(ctx, auth, level="WARNING")
    assert len(rows) == 1 and rows[0]["event"] == "SL"


def test_filter_by_event(ctx, auth):
    rows = _get(ctx, auth, event="ENTRY")
    assert len(rows) == 1 and rows[0]["event"] == "ENTRY"


def test_filter_by_date(ctx, auth):
    rows = _get(ctx, auth, log_date="2026-08-10")
    assert len(rows) == 2 and {r["event"] for r in rows} == {"ENTRY", "SL"}
    rows = _get(ctx, auth, log_date="2026-08-11")
    assert len(rows) == 1 and rows[0]["event"] == "EXIT"


def test_combined_level_and_date_filter(ctx, auth):
    rows = _get(ctx, auth, level="INFO", log_date="2026-08-10")
    assert len(rows) == 1 and rows[0]["event"] == "ENTRY"


def test_bad_date_400(ctx, auth):
    r = ctx.get(
        "/api/logs",
        params={"algo_id": "algo1", "server_id": "ec2-1", "log_date": "not-a-date"},
        headers=auth,
    )
    assert r.status_code == 400


def test_details_json_round_trips(ctx, auth):
    rows = _get(ctx, auth, event="ENTRY")
    assert rows[0]["details"] == {"strike": 24750}
