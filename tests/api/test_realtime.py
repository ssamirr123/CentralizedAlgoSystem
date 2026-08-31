"""Stage 19 -- realtime WebSocket stream: auth, hello, event fan-out,
ping/pong, dedup (monotonic seq), graceful auth failure."""
from __future__ import annotations

import pytest

from trading.api.realtime import events
from trading.api.realtime.bus import bus


@pytest.fixture
def admin_token(client, make_user, token_for):
    make_user(username="rt_admin", role="admin")
    return token_for("rt_admin")


@pytest.fixture
def env(seed_server, seed_algo):
    srv = seed_server(name="ec2-1", ec2_instance_id="i-x", region="ap-south-1", status="RUNNING")
    seed_algo(srv, name="ex", status="STOPPED")
    return srv


def _open(client, token):
    return client.websocket_connect("/api/ws", subprotocols=["cas.realtime.v1", f"bearer.{token}"])


def test_hello_frame_on_connect(client, admin_token):
    with _open(client, admin_token) as ws:
        hello = ws.receive_json()
        assert hello["type"] == events.HELLO
        assert hello["user"]["username"] == "rt_admin"
        assert set(hello["event_types"]) == set(events.MONITORING_TYPES)
        assert hello["ping_interval"] > 0


def test_no_token_is_rejected_cleanly(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws", subprotocols=["cas.realtime.v1"]) as ws:
            err = ws.receive_json()
            assert err["type"] == events.ERROR and err["code"] == "unauthorized"
            ws.receive_json()  # -> raises on close


def test_bad_token_is_rejected(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws", subprotocols=["cas.realtime.v1", "bearer.garbage"]) as ws:
            assert ws.receive_json()["code"] == "unauthorized"
            ws.receive_json()


def test_viewer_may_connect(client, make_user, token_for):
    make_user(username="rt_viewer", role="viewer")
    with _open(client, token_for("rt_viewer")) as ws:
        assert ws.receive_json()["type"] == events.HELLO


def test_heartbeat_post_fans_out(client, admin_token, service_auth, env):
    with _open(client, admin_token) as ws:
        ws.receive_json()  # hello
        r = client.post("/api/heartbeat", headers=service_auth,
                        json={"algo_id": "ex", "server_id": "ec2-1", "status": "RUNNING"})
        assert r.status_code == 200
        seen = [ws.receive_json() for _ in range(3)]
        types = {e["type"] for e in seen}
        assert events.HEARTBEAT in types
        assert events.STRATEGY_STATUS in types
        assert events.ALERT in types  # STOPPED -> RUNNING transition
        # monotonic, unique seq -> client-side dedupe is trivial
        seqs = [e["seq"] for e in seen]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)


def test_trade_and_position_and_pnl_events(client, admin_token, service_auth, env):
    with _open(client, admin_token) as ws:
        ws.receive_json()
        client.post("/api/trades", headers=service_auth, json={
            "algo_id": "ex", "server_id": "ec2-1", "symbol": "NIFTY", "side": "BUY",
            "quantity": 50, "price": 100.5,
        })
        assert ws.receive_json()["type"] == events.TRADE
        client.post("/api/positions", headers=service_auth, json={
            "algo_id": "ex", "server_id": "ec2-1", "symbol": "NIFTY",
            "quantity": 50, "average_price": 100.5,
        })
        assert ws.receive_json()["type"] == events.POSITION
        client.post("/api/pnl", headers=service_auth, json={
            "algo_id": "ex", "server_id": "ec2-1", "pnl": 1234.5, "trade_count": 1,
        })
        assert ws.receive_json()["type"] == events.PNL


def test_error_log_becomes_alert(client, admin_token, service_auth, env):
    with _open(client, admin_token) as ws:
        ws.receive_json()
        client.post("/api/logs", headers=service_auth, json={
            "algo_id": "ex", "server_id": "ec2-1", "level": "ERROR", "event": "BOOM",
        })
        ev = ws.receive_json()
        assert ev["type"] == events.ALERT and ev["data"]["severity"] == "critical"


def test_client_ping_gets_pong(client, admin_token):
    with _open(client, admin_token) as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "ping"})
        # drain until we see the pong (other events may interleave)
        for _ in range(5):
            m = ws.receive_json()
            if m["type"] == events.PONG:
                break
        else:
            pytest.fail("no pong received")


def test_two_clients_both_receive(client, admin_token, service_auth, env):
    with _open(client, admin_token) as a, _open(client, admin_token) as b:
        a.receive_json(); b.receive_json()  # hellos
        client.post("/api/heartbeat", headers=service_auth,
                    json={"algo_id": "ex", "server_id": "ec2-1", "status": "RUNNING"})
        assert a.receive_json()["type"] == events.HEARTBEAT
        assert b.receive_json()["type"] == events.HEARTBEAT


def test_bus_has_no_subscribers_after_disconnect(client, admin_token):
    with _open(client, admin_token) as ws:
        ws.receive_json()
        assert bus.subscriber_count >= 1
    assert bus.subscriber_count == 0
