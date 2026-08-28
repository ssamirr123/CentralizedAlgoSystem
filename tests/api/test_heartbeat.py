"""POST /api/heartbeat + GET /api/algos last_heartbeat enrichment.
Ported from trading/api/test_heartbeat_local.py."""
from __future__ import annotations

import pytest

from trading.database import models


@pytest.fixture
def server(seed_server):
    return seed_server(name="ec2-1", ec2_instance_id="i-test", status="RUNNING")


def test_heartbeat_requires_auth(client, server):
    r = client.post("/api/heartbeat", json={"algo_id": "x", "server_id": "ec2-1", "status": "RUNNING"})
    assert r.status_code == 401


def test_heartbeat_unknown_server_404(client, server, auth):
    r = client.post(
        "/api/heartbeat",
        json={"algo_id": "x", "server_id": "nonexistent", "status": "RUNNING"},
        headers=auth,
    )
    assert r.status_code == 404


def test_heartbeat_unregistered_algo_404(client, server, auth):
    r = client.post(
        "/api/heartbeat",
        json={"algo_id": "example_strategy", "server_id": "ec2-1", "status": "RUNNING"},
        headers=auth,
    )
    assert r.status_code == 404


def test_heartbeat_full_cycle(client, server, auth, db_session):
    r = client.post("/api/algos", json={"algo_id": "example_strategy", "server_id": "ec2-1"}, headers=auth)
    assert r.status_code == 201, r.text

    r = client.post(
        "/api/heartbeat",
        json={
            "algo_id": "example_strategy", "server_id": "ec2-1", "status": "RUNNING",
            "cpu": 12.5, "memory": 8.2, "pnl": 1500.0, "position": "SHORT_STRADDLE",
        },
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json() == {"success": True, "algo_id": "example_strategy", "server_id": "ec2-1"}

    # the request wrote via its own session; drop this session's cached copies
    db_session.expire_all()
    algo = db_session.query(models.Algo).filter(models.Algo.name == "example_strategy").one()
    assert algo.status == "RUNNING"
    hb_rows = db_session.query(models.Heartbeat).filter(models.Heartbeat.algo_id == algo.id).all()
    assert len(hb_rows) == 1
    assert (hb_rows[0].cpu, hb_rows[0].memory, hb_rows[0].pnl, hb_rows[0].position) == (
        12.5, 8.2, 1500.0, "SHORT_STRADDLE"
    )

    server_row = db_session.query(models.Server).filter(models.Server.name == "ec2-1").one()
    assert server_row.last_heartbeat is not None
    assert server_row.status == "RUNNING"  # not re-derived from algo heartbeat

    algos = client.get("/api/algos", headers=auth).json()
    assert len(algos) == 1
    assert algos[0]["last_heartbeat"] is not None

    # second heartbeat: appended (history), algo.status re-set
    r = client.post(
        "/api/heartbeat",
        json={"algo_id": "example_strategy", "server_id": "ec2-1", "status": "ERROR"},
        headers=auth,
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.query(models.Heartbeat).filter(models.Heartbeat.algo_id == algo.id).count() == 2
    assert db_session.query(models.Algo).filter(models.Algo.name == "example_strategy").one().status == "ERROR"
