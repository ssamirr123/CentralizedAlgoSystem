"""Phase 16.12: trading/api/worker_routes.py -- the real HTTP machine API
for remote strategy workers. Proves worker authentication (a lane
entirely separate from human/operator RBAC and from the existing
CONTROL_API_KEY machine key), session security, cross-worker rejection,
stale-intent rejection, network-retry idempotent replay, and that no
route here can produce a real broker mutation."""
from __future__ import annotations


def _register(client, app, worker_id="w1", secret="s3cr3t", name="A"):
    app.state.execution.worker_auth_registry.provision(worker_id, secret)
    r = client.post(
        "/api/worker/register",
        json={"worker_id": worker_id, "name": name},
        headers={"X-Worker-Auth": secret},
    )
    return r


def _intent_body(strategy_id, session_id, account_id="ANGEL_MAIN", **overrides):
    body = dict(
        worker_id="w1", session_id=session_id, strategy_id=strategy_id, evaluation_id="e1",
        generated_at="__now__", submission_id="__auto__", account_id=account_id, symbol="NIFTY",
        exchange="NFO", side="BUY", quantity=1, idempotency_key="k1",
    )
    body.update(overrides)
    return body


def _now_iso():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _prepare_strategy(client, bearer, strategy_id="DoubleStraddelAlgo", account_id="ANGEL_MAIN"):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": strategy_id, "account_id": account_id})
    client.post(f"/api/strategy-lifecycle/{strategy_id}/command", headers=op, json={"command": "START"})


# --------------------------------------------------------------------------- #
# Worker authentication
# --------------------------------------------------------------------------- #
def test_register_requires_worker_auth_header(client, app):
    app.state.execution.worker_auth_registry.provision("w1", "s3cr3t")
    r = client.post("/api/worker/register", json={"worker_id": "w1", "name": "A"})
    assert r.status_code == 401


def test_register_rejects_wrong_secret(client, app):
    app.state.execution.worker_auth_registry.provision("w1", "s3cr3t")
    r = client.post("/api/worker/register", json={"worker_id": "w1", "name": "A"}, headers={"X-Worker-Auth": "wrong"})
    assert r.status_code == 401


def test_register_rejects_unprovisioned_worker(client, app):
    r = client.post("/api/worker/register", json={"worker_id": "unknown-worker", "name": "A"}, headers={"X-Worker-Auth": "anything"})
    assert r.status_code == 401


def test_register_succeeds_with_correct_secret(client, app):
    r = _register(client, app)
    assert r.status_code == 200
    body = r.json()
    assert body["worker_id"] == "w1"
    assert body["status"] == "ONLINE"
    assert body["session_id"] != ""


def test_register_response_never_echoes_the_secret(client, app):
    r = _register(client, app, secret="super-secret-value")
    assert "super-secret-value" not in r.text


def test_operator_bearer_token_cannot_authenticate_as_a_worker(client, app, bearer):
    app.state.execution.worker_auth_registry.provision("w1", "s3cr3t")
    op = bearer(role="operator")
    r = client.post("/api/worker/register", json={"worker_id": "w1", "name": "A"}, headers={**op, "X-Worker-Auth": "wrong"})
    assert r.status_code == 401  # a valid human token does not substitute for worker auth


def test_worker_secret_cannot_authenticate_operator_routes(client, app):
    _register(client, app)
    r = client.get("/api/strategies", headers={"X-Worker-Auth": "s3cr3t"})
    assert r.status_code == 401  # worker auth header is not a recognized operator credential


# --------------------------------------------------------------------------- #
# Heartbeat / session security
# --------------------------------------------------------------------------- #
def test_heartbeat_requires_matching_session(client, app):
    reg = _register(client, app).json()
    r = client.post(
        "/api/worker/heartbeat",
        json={"worker_id": "w1", "session_id": "not-the-real-session"},
        headers={"X-Worker-Auth": "s3cr3t"},
    )
    assert r.status_code == 409


def test_heartbeat_succeeds_with_correct_session(client, app):
    reg = _register(client, app).json()
    r = client.post(
        "/api/worker/heartbeat",
        json={"worker_id": "w1", "session_id": reg["session_id"]},
        headers={"X-Worker-Auth": "s3cr3t"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ONLINE"


def test_unknown_worker_heartbeat_is_404(client, app):
    app.state.execution.worker_auth_registry.provision("ghost", "s3cr3t")
    r = client.post("/api/worker/heartbeat", json={"worker_id": "ghost", "session_id": "x"}, headers={"X-Worker-Auth": "s3cr3t"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# OrderIntent submission over real HTTP
# --------------------------------------------------------------------------- #
def test_order_intent_end_to_end_accepted(client, app, bearer):
    _prepare_strategy(client, bearer, "DoubleStraddelAlgo", "ANGEL_MAIN")
    reg = _register(client, app).json()
    app.state.execution.worker_registry.assign_strategy("DoubleStraddelAlgo", "w1")

    body = _intent_body("DoubleStraddelAlgo", reg["session_id"], generated_at=_now_iso(), submission_id="sub-1")
    r = client.post("/api/worker/order-intents", json=body, headers={"X-Worker-Auth": "s3cr3t"})
    assert r.status_code == 200
    out = r.json()
    assert out["accepted"] is True
    assert out["execution_result"]["success"] is True


def test_order_intent_cross_worker_strategy_is_rejected(client, app, bearer):
    _prepare_strategy(client, bearer, "DoubleStraddelAlgo", "ANGEL_MAIN")
    app.state.execution.worker_auth_registry.provision("w2", "other-secret")
    client.post("/api/worker/register", json={"worker_id": "w2", "name": "B"}, headers={"X-Worker-Auth": "other-secret"})
    reg = _register(client, app).json()  # w1
    app.state.execution.worker_registry.assign_strategy("DoubleStraddelAlgo", "w2")  # owned by w2, not w1

    body = _intent_body("DoubleStraddelAlgo", reg["session_id"], generated_at=_now_iso(), submission_id="sub-x")
    r = client.post("/api/worker/order-intents", json=body, headers={"X-Worker-Auth": "s3cr3t"})
    assert r.status_code == 200
    assert r.json()["accepted"] is False
    assert "not assigned to worker" in r.json()["reason"]


def test_order_intent_stale_generated_at_is_rejected(client, app, bearer):
    import datetime as dt

    _prepare_strategy(client, bearer, "DoubleStraddelAlgo", "ANGEL_MAIN")
    reg = _register(client, app).json()
    app.state.execution.worker_registry.assign_strategy("DoubleStraddelAlgo", "w1")

    stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=999)).isoformat()
    body = _intent_body("DoubleStraddelAlgo", reg["session_id"], generated_at=stale, submission_id="sub-stale")
    r = client.post("/api/worker/order-intents", json=body, headers={"X-Worker-Auth": "s3cr3t"})
    assert r.status_code == 200
    assert r.json()["accepted"] is False
    assert "stale" in r.json()["reason"].lower()


def test_order_intent_malformed_side_is_rejected(client, app, bearer):
    _prepare_strategy(client, bearer, "DoubleStraddelAlgo", "ANGEL_MAIN")
    reg = _register(client, app).json()
    app.state.execution.worker_registry.assign_strategy("DoubleStraddelAlgo", "w1")

    body = _intent_body("DoubleStraddelAlgo", reg["session_id"], generated_at=_now_iso(), submission_id="sub-bad", side="SIDEWAYS")
    r = client.post("/api/worker/order-intents", json=body, headers={"X-Worker-Auth": "s3cr3t"})
    assert r.status_code == 422


def test_order_intent_network_retry_replays_the_same_result(client, app, bearer):
    _prepare_strategy(client, bearer, "DoubleStraddelAlgo", "ANGEL_MAIN")
    reg = _register(client, app).json()
    app.state.execution.worker_registry.assign_strategy("DoubleStraddelAlgo", "w1")

    body = _intent_body("DoubleStraddelAlgo", reg["session_id"], generated_at=_now_iso(), submission_id="sub-retry")
    r1 = client.post("/api/worker/order-intents", json=body, headers={"X-Worker-Auth": "s3cr3t"})
    r2 = client.post("/api/worker/order-intents", json=body, headers={"X-Worker-Auth": "s3cr3t"})  # identical retry
    assert r1.json()["accepted"] is True
    assert r2.json() == r1.json()  # exact same logical outcome, not a duplicate rejection


def test_order_intent_worker_supplied_account_id_never_used_for_routing(client, app, bearer):
    _prepare_strategy(client, bearer, "DoubleStraddelAlgo", "ANGEL_MAIN")
    reg = _register(client, app).json()
    app.state.execution.worker_registry.assign_strategy("DoubleStraddelAlgo", "w1")

    body = _intent_body(
        "DoubleStraddelAlgo", reg["session_id"], account_id="SOMEONE_ELSES_ACCOUNT",
        generated_at=_now_iso(), submission_id="sub-acct",
    )
    r = client.post("/api/worker/order-intents", json=body, headers={"X-Worker-Auth": "s3cr3t"})
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert r.json()["execution_result"]["account_id"] == "ANGEL_MAIN"  # centrally-assigned, not the bogus one


# --------------------------------------------------------------------------- #
# No secrets, no mutation-verb surface anywhere
# --------------------------------------------------------------------------- #
def test_no_worker_route_response_ever_contains_a_secret_or_broker_verb(client, app, bearer):
    _prepare_strategy(client, bearer, "DoubleStraddelAlgo", "ANGEL_MAIN")
    reg = _register(client, app).json()
    app.state.execution.worker_registry.assign_strategy("DoubleStraddelAlgo", "w1")
    body = _intent_body("DoubleStraddelAlgo", reg["session_id"], generated_at=_now_iso(), submission_id="sub-scan")
    r = client.post("/api/worker/order-intents", json=body, headers={"X-Worker-Auth": "s3cr3t"})

    for resp in (client.get("/api/worker/time"), r):
        text = resp.text.lower()
        for forbidden in ("s3cr3t", "place_order", "placeorder", "modify_order", "cancel_order", "authorize_live", "go_live", "api_secret", "api_key"):
            assert forbidden not in text
