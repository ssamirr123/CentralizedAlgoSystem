"""POST/GET /api/positions, /api/trades, /api/pnl.
Ported from trading/api/test_positions_pnl_local.py."""
from __future__ import annotations

import pytest


@pytest.fixture
def ctx(client, auth, seed_server):
    seed_server(name="ec2-1", ec2_instance_id="i-test", status="RUNNING")
    assert client.post("/api/algos", json={"algo_id": "a1", "server_id": "ec2-1"}, headers=auth).status_code == 201
    return client


P = {"algo_id": "a1", "server_id": "ec2-1"}


def test_position_requires_auth(ctx):
    r = ctx.post("/api/positions", json={**P, "symbol": "X", "quantity": 1, "average_price": 1.0})
    assert r.status_code == 401


def test_position_create_upsert_close(ctx, auth):
    r = ctx.post("/api/positions",
                 json={**P, "symbol": "NIFTY", "quantity": 50, "average_price": 24700.0,
                       "last_price": 24750.0, "pnl": 2500.0}, headers=auth)
    assert r.status_code == 200 and r.json() == {"success": True, "closed": False}

    rows = ctx.get("/api/positions", params=P, headers=auth).json()
    assert len(rows) == 1
    assert (rows[0]["symbol"], rows[0]["quantity"], rows[0]["last_price"], rows[0]["pnl"]) == (
        "NIFTY", 50, 24750.0, 2500.0
    )

    # same symbol -> upsert
    ctx.post("/api/positions",
             json={**P, "symbol": "NIFTY", "quantity": 75, "average_price": 24710.0}, headers=auth)
    rows = ctx.get("/api/positions", params=P, headers=auth).json()
    assert len(rows) == 1 and rows[0]["quantity"] == 75

    # quantity 0 -> close (delete row)
    r = ctx.post("/api/positions",
                 json={**P, "symbol": "NIFTY", "quantity": 0, "average_price": 24710.0}, headers=auth)
    assert r.json() == {"success": True, "closed": True}
    assert ctx.get("/api/positions", params=P, headers=auth).json() == []


def test_trades_are_insert_only_history(ctx, auth):
    for i, side in enumerate(["BUY", "SELL"]):
        r = ctx.post("/api/trades",
                     json={**P, "symbol": "NIFTY", "side": side, "quantity": 50,
                           "price": 24700.0 + i, "order_id": f"PAPER-{i}"}, headers=auth)
        assert r.status_code == 200 and r.json() == {"success": True}

    rows = ctx.get("/api/trades", params=P, headers=auth).json()
    assert len(rows) == 2 and {t["side"] for t in rows} == {"BUY", "SELL"}

    ctx.post("/api/trades",
             json={**P, "symbol": "NIFTY", "side": "BUY", "quantity": 25, "price": 24705.0}, headers=auth)
    assert len(ctx.get("/api/trades", params=P, headers=auth).json()) == 3


def test_daily_pnl_upsert_by_date(ctx, auth):
    ctx.post("/api/pnl", json={**P, "pnl": 1500.0, "trade_count": 3, "pnl_date": "2026-08-12"}, headers=auth)
    rows = ctx.get("/api/pnl", params=P, headers=auth).json()
    assert len(rows) == 1 and rows[0]["pnl"] == 1500.0 and rows[0]["trade_count"] == 3

    # same date -> upsert
    ctx.post("/api/pnl", json={**P, "pnl": 2200.0, "trade_count": 4, "pnl_date": "2026-08-12"}, headers=auth)
    rows = ctx.get("/api/pnl", params=P, headers=auth).json()
    assert len(rows) == 1 and rows[0]["pnl"] == 2200.0

    # different date -> new row
    ctx.post("/api/pnl", json={**P, "pnl": 500.0, "trade_count": 1, "pnl_date": "2026-08-13"}, headers=auth)
    assert len(ctx.get("/api/pnl", params=P, headers=auth).json()) == 2

    # omit date -> defaults to today, still 200
    assert ctx.post("/api/pnl", json={**P, "pnl": 999.0, "trade_count": 1}, headers=auth).status_code == 200
