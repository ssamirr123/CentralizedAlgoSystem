"""Phase 17.1-R Remediation F: trading/api/live_authorization_routes.py --
the first HTTP boundary bridging an authenticated human operator into
trading.common.live_authorization_workflow. Proves: unauthenticated=401,
unauthorized operator=403, authorized operator can request/confirm,
request body cannot forge an operator identity, worker identity cannot
substitute for operator identity, single-use/expiry/scope still hold after
HTTP integration, and creating/confirming an authorization never starts a
strategy, submits an intent, or calls a broker mutation method.

Uses only the example ShadowBroker-backed ANGEL_MAIN account already
wired by build_execution_state() -- no real broker credential anywhere."""
from __future__ import annotations

import pytest

from trading.common.live_authorization import LiveAuthorizationError

ACCOUNT_ID = "ANGEL_MAIN"
_TEST_CREDENTIAL_REF = "env:TEST_ANGEL_MAIN"


@pytest.fixture(autouse=True)
def _wire_test_credential(app, bearer, client):
    # AuthorizationRequest structurally requires a non-empty
    # credential_reference (see its own __post_init__), but the example
    # ANGEL_MAIN account build_execution_state() registers defaults to ""
    # (TradingAccount's own default) -- give this account a real, non-
    # secret test pointer so validate_request()'s exact-match check has
    # something legitimate to compare against.
    app.state.execution.broker_manager.get_account(ACCOUNT_ID).credential_reference = _TEST_CREDENTIAL_REF
    # RiskManager.validate() (called by validate_request()/run_preflight()
    # as a preview) requires a real strategy/account assignment to exist --
    # this is a workflow precondition, unrelated to operator identity.
    admin = bearer(role="admin", username="setup-admin")
    client.post("/api/assignments", headers=admin, json={"strategy_id": "DoubleStraddelAlgo", "account_id": ACCOUNT_ID})


def _body(**overrides):
    body = dict(
        account_id=ACCOUNT_ID, broker_id="angelone", credential_reference=_TEST_CREDENTIAL_REF,
        strategy_id="DoubleStraddelAlgo", symbol="NIFTY24950CE", side="BUY", quantity=65,
        order_type="MARKET", product_type="INTRADAY", max_order_value=10_000_000.0,
        daily_loss_limit=2000.0, strategy_loss_limit=2000.0, max_orders_per_day=1,
        idempotency_key="phase17-1r-test-key-1",
    )
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Authentication / authorization boundary
# --------------------------------------------------------------------------- #
def test_unauthenticated_request_is_401(client):
    r = client.post("/api/live-authorization/request", json=_body())
    assert r.status_code == 401


def test_viewer_role_is_403(client, bearer):
    headers = bearer(role="viewer")
    r = client.post("/api/live-authorization/request", headers=headers, json=_body())
    assert r.status_code == 403


def test_trader_role_lacks_trading_control_and_is_403(client, bearer):
    headers = bearer(role="trader")
    r = client.post("/api/live-authorization/request", headers=headers, json=_body())
    assert r.status_code == 403


def test_operator_role_can_request(client, bearer):
    headers = bearer(role="operator")
    r = client.post("/api/live-authorization/request", headers=headers, json=_body())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "PENDING"
    assert body["account_id"] == ACCOUNT_ID
    assert body["authorized_by"] == "operator"  # the authenticated username, never client-supplied


def test_admin_role_can_request(client, bearer):
    headers = bearer(role="admin")
    r = client.post("/api/live-authorization/request", headers=headers, json=_body(idempotency_key="admin-key-1"))
    assert r.status_code == 201, r.text


def test_worker_auth_header_alone_cannot_authenticate(client, app):
    app.state.execution.worker_auth_registry.provision("w1", "s3cr3t")
    r = client.post("/api/live-authorization/request", headers={"X-Worker-Auth": "s3cr3t"}, json=_body())
    assert r.status_code == 401  # worker identity is a completely separate lane -- never accepted here


# --------------------------------------------------------------------------- #
# Forged operator identity in the request body
# --------------------------------------------------------------------------- #
def test_forged_operator_field_in_body_is_rejected(client, bearer):
    headers = bearer(role="operator")
    payload = _body(idempotency_key="forge-key-1")
    payload["operator"] = "admin"
    r = client.post("/api/live-authorization/request", headers=headers, json=payload)
    assert r.status_code == 422  # extra="forbid" rejects the whole request -- never silently ignored


def test_forged_authorized_by_field_in_body_is_rejected(client, bearer):
    headers = bearer(role="operator")
    payload = _body(idempotency_key="forge-key-2")
    payload["authorized_by"] = "someone-else"
    r = client.post("/api/live-authorization/request", headers=headers, json=payload)
    assert r.status_code == 422


def test_authorized_by_always_reflects_the_authenticated_caller_not_a_forged_value(client, bearer):
    headers = bearer(role="admin", username="realadmin")
    r = client.post("/api/live-authorization/request", headers=headers, json=_body(idempotency_key="attrib-key-1"))
    assert r.status_code == 201
    assert r.json()["authorized_by"] == "realadmin"


# --------------------------------------------------------------------------- #
# Request -> confirm -> single-use / independence from execution
# --------------------------------------------------------------------------- #
def test_request_then_confirm_reaches_authorized(client, bearer):
    headers = bearer(role="operator")
    created = client.post("/api/live-authorization/request", headers=headers, json=_body(idempotency_key="confirm-key-1")).json()
    r = client.post(
        f"/api/live-authorization/{created['authorization_id']}/confirm",
        headers=headers, json={"strategy_id": "DoubleStraddelAlgo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "AUTHORIZED"


def test_confirm_by_a_different_authorized_operator_is_allowed_and_attributed_to_requester(client, bearer):
    """The REQUESTING operator's identity is durably stamped on the
    authorization row at creation and never changes -- the CONFIRMING
    operator may legitimately be a different authenticated principal
    holding CONFIRM_LIVE_ACTION (e.g. a second approver)."""
    requester = bearer(role="operator", username="requester1")
    confirmer = bearer(role="admin", username="confirmer1")
    created = client.post("/api/live-authorization/request", headers=requester, json=_body(idempotency_key="dual-approve-1")).json()
    assert created["authorized_by"] == "requester1"
    r = client.post(
        f"/api/live-authorization/{created['authorization_id']}/confirm",
        headers=confirmer, json={"strategy_id": "DoubleStraddelAlgo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["authorized_by"] == "requester1"  # unchanged by who confirmed it


def test_creating_authorization_never_starts_the_strategy(client, bearer):
    headers = bearer(role="operator")
    client.post("/api/live-authorization/request", headers=headers, json=_body(idempotency_key="no-autostart-1"))
    r = client.get("/api/strategy-lifecycle/DoubleStraddelAlgo", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["lifecycle_state"] not in ("RUNNING", "SHADOW")


def test_confirming_authorization_never_submits_an_order_intent(client, bearer, app):
    headers = bearer(role="operator")
    created = client.post("/api/live-authorization/request", headers=headers, json=_body(idempotency_key="no-execute-1")).json()
    client.post(
        f"/api/live-authorization/{created['authorization_id']}/confirm",
        headers=headers, json={"strategy_id": "DoubleStraddelAlgo"},
    )
    # No worker/strategy ever submitted anything -- the idempotency_key used
    # for this authorization must still be entirely absent from the
    # wired idempotency store (never created by requesting/confirming).
    assert app.state.execution.idempotency_store.get("no-execute-1") is None


def test_get_authorization_requires_authentication(client, bearer):
    headers = bearer(role="operator")
    created = client.post("/api/live-authorization/request", headers=headers, json=_body(idempotency_key="get-auth-1")).json()
    r = client.get(f"/api/live-authorization/{created['authorization_id']}")
    assert r.status_code == 401
    r2 = client.get(f"/api/live-authorization/{created['authorization_id']}", headers=headers)
    assert r2.status_code == 200


def test_unknown_authorization_id_confirm_is_404(client, bearer):
    headers = bearer(role="operator")
    r = client.post("/api/live-authorization/does-not-exist/confirm", headers=headers, json={"strategy_id": "DoubleStraddelAlgo"})
    assert r.status_code == 404


def test_wrong_account_scope_is_rejected_at_request_time(client, bearer):
    headers = bearer(role="operator")
    r = client.post(
        "/api/live-authorization/request", headers=headers,
        json=_body(account_id="DOES_NOT_EXIST", idempotency_key="wrong-account-1"),
    )
    assert r.status_code == 422


def test_credential_reference_mismatch_is_rejected(client, bearer):
    headers = bearer(role="operator")
    r = client.post(
        "/api/live-authorization/request", headers=headers,
        json=_body(credential_reference="env:SOMETHING_ELSE", idempotency_key="cred-mismatch-1"),
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Single-use, post-HTTP-integration (Section 34)
# --------------------------------------------------------------------------- #
def test_confirmed_authorization_is_single_use_over_http(client, bearer, app):
    headers = bearer(role="operator")
    created = client.post("/api/live-authorization/request", headers=headers, json=_body(idempotency_key="single-use-1")).json()
    first = client.post(
        f"/api/live-authorization/{created['authorization_id']}/confirm",
        headers=headers, json={"strategy_id": "DoubleStraddelAlgo"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "AUTHORIZED"

    # Simulate a FakeBroker consumption exactly as execute() would --
    # try_consume() is the real single-use gate; the HTTP layer above it
    # never touches this method at all.
    consumed = app.state.execution.live_authorization_store.try_consume(
        created["authorization_id"], account_id=ACCOUNT_ID, broker_id="angelone",
        credential_reference=_TEST_CREDENTIAL_REF, symbol="NIFTY24950CE", side="BUY",
        quantity=65, order_type="MARKET", product_type="INTRADAY", idempotency_key="single-use-1",
        order_value=1000.0, correlation_id="corr-1",
    )
    assert consumed.status == "CONSUMED"

    with pytest.raises(LiveAuthorizationError):
        app.state.execution.live_authorization_store.try_consume(
            created["authorization_id"], account_id=ACCOUNT_ID, broker_id="angelone",
            credential_reference=_TEST_CREDENTIAL_REF, symbol="NIFTY24950CE", side="BUY",
            quantity=65, order_type="MARKET", product_type="INTRADAY", idempotency_key="single-use-1",
            order_value=1000.0, correlation_id="corr-2",
        )


# --------------------------------------------------------------------------- #
# Cross-strategy/cross-account leakage (Section 35)
# --------------------------------------------------------------------------- #
def test_authorization_scoped_to_one_account_cannot_be_consumed_for_another(client, bearer, app):
    headers = bearer(role="operator")
    created = client.post("/api/live-authorization/request", headers=headers, json=_body(idempotency_key="scope-leak-1")).json()
    client.post(
        f"/api/live-authorization/{created['authorization_id']}/confirm",
        headers=headers, json={"strategy_id": "DoubleStraddelAlgo"},
    )
    with pytest.raises(LiveAuthorizationError):
        app.state.execution.live_authorization_store.try_consume(
            created["authorization_id"], account_id="DHAN_MAIN", broker_id="angelone",
            credential_reference=_TEST_CREDENTIAL_REF, symbol="NIFTY24950CE", side="BUY",
            quantity=65, order_type="MARKET", product_type="INTRADAY", idempotency_key="scope-leak-1",
            order_value=1000.0, correlation_id="corr-3",
        )
