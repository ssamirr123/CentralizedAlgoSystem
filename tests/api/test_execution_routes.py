"""Phase 11: Trading Control Center backend -- strategies, accounts,
brokers, assignments, execution modes, risk (status/limits/kill-switch),
execution orders/positions/pnl, and system status.

Covers: authentication (401 with no credentials), authorization (RBAC per
endpoint, matching the existing role bundles), validation (unknown
strategy/account -> 404, bad execution_mode -> 422, invalid/unavailable
assignment -> 409/422), audit logging (every mutation writes a row to the
existing audit_log table), and the explicit safety requirements (no
broker credential in any response, no live order ever placed).
"""
from __future__ import annotations

from trading.database import models

STRATEGY_IDS = {"DoubleStraddelAlgo", "CombinedVwapNifty", "Vwap_Algo_Nifty_hedge"}


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_unauthenticated_request_is_401(client):
    r = client.get("/api/strategies")
    assert r.status_code == 401


def test_service_key_can_read(client, service_auth):
    r = client.get("/api/strategies", headers=service_auth)
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# STRATEGIES
# --------------------------------------------------------------------------- #
def test_list_strategies_returns_all_three(client, viewer_auth):
    r = client.get("/api/strategies", headers=viewer_auth)
    assert r.status_code == 200
    ids = {s["strategy_id"] for s in r.json()}
    assert ids == STRATEGY_IDS
    for s in r.json():
        assert s["status"] == "disabled"
        assert s["execution_mode"] == "SHADOW"


def test_get_single_strategy(client, viewer_auth):
    r = client.get("/api/strategies/DoubleStraddelAlgo", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json()["strategy_id"] == "DoubleStraddelAlgo"


def test_get_unknown_strategy_is_404(client, viewer_auth):
    r = client.get("/api/strategies/NoSuchStrategy", headers=viewer_auth)
    assert r.status_code == 404


def test_start_then_stop_a_strategy(client, trader_auth):
    r = client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    assert r.status_code == 200
    assert r.json()["status"] == "shadow"

    r = client.post("/api/strategies/CombinedVwapNifty/stop", headers=trader_auth)
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"


def test_starting_an_already_running_strategy_is_409(client, trader_auth):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    r = client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    assert r.status_code == 409


def test_stopping_a_disabled_strategy_is_409(client, trader_auth):
    r = client.post("/api/strategies/CombinedVwapNifty/stop", headers=trader_auth)
    assert r.status_code == 409


def test_start_unknown_strategy_is_404(client, trader_auth):
    r = client.post("/api/strategies/NoSuchStrategy/start", headers=trader_auth)
    assert r.status_code == 404


# --- RBAC on strategy start/stop -------------------------------------------- #
def test_viewer_cannot_start_a_strategy(client, viewer_auth):
    r = client.post("/api/strategies/CombinedVwapNifty/start", headers=viewer_auth)
    assert r.status_code == 403


def test_viewer_cannot_stop_a_strategy(client, trader_auth, viewer_auth):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    r = client.post("/api/strategies/CombinedVwapNifty/stop", headers=viewer_auth)
    assert r.status_code == 403


def test_admin_can_start_and_stop(client, admin_auth):
    assert client.post("/api/strategies/CombinedVwapNifty/start", headers=admin_auth).status_code == 200
    assert client.post("/api/strategies/CombinedVwapNifty/stop", headers=admin_auth).status_code == 200


# --------------------------------------------------------------------------- #
# ACCOUNTS / BROKERS
# --------------------------------------------------------------------------- #
def test_list_accounts_returns_the_three_example_accounts(client, viewer_auth):
    r = client.get("/api/accounts", headers=viewer_auth)
    assert r.status_code == 200
    ids = {a["account_id"] for a in r.json()}
    assert ids == {"ANGEL_MAIN", "DHAN_MAIN", "ICICI_MAIN"}
    for a in r.json():
        assert a["execution_mode"] == "SHADOW"


def test_account_response_never_includes_a_credential_field(client, viewer_auth):
    r = client.get("/api/accounts", headers=viewer_auth)
    for account in r.json():
        assert "credential_reference" not in account
        assert "broker_client" not in account
        for key in account:
            assert "secret" not in key.lower()
            assert "password" not in key.lower()
            assert "token" not in key.lower()


def test_get_single_account(client, viewer_auth):
    r = client.get("/api/accounts/ANGEL_MAIN", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json()["broker_id"] == "angelone"


def test_get_unknown_account_is_404(client, viewer_auth):
    r = client.get("/api/accounts/NO_SUCH_ACCOUNT", headers=viewer_auth)
    assert r.status_code == 404


def test_list_brokers_reports_dhan_and_icici_unavailable(client, viewer_auth):
    r = client.get("/api/brokers", headers=viewer_auth)
    assert r.status_code == 200
    by_id = {b["broker_id"]: b for b in r.json()}
    assert by_id["angelone"]["available"] is True
    assert by_id["dhan"]["available"] is False
    assert by_id["icici_breeze"]["available"] is False
    assert by_id["angelone"]["account_count"] == 1


# --------------------------------------------------------------------------- #
# ASSIGNMENTS
# --------------------------------------------------------------------------- #
def test_list_assignments_starts_empty(client, viewer_auth):
    r = client.get("/api/assignments", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json() == []


def test_trader_lacks_trading_control_for_assignments(client, trader_auth):
    r = client.post("/api/assignments", headers=trader_auth,
                    json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    assert r.status_code == 403  # trader lacks TRADING_CONTROL


def test_create_assignment_requires_trading_control(client, bearer):
    op = bearer(role="operator")
    r = client.post("/api/assignments", headers=op,
                    json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    assert r.status_code == 201
    assert r.json()["account_id"] == "ANGEL_MAIN"
    assert r.json()["execution_mode"] == "SHADOW"


def test_get_assignment_after_creation(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    r = client.get("/api/assignments/DoubleStraddelAlgo", headers=op)
    assert r.status_code == 200
    assert r.json()["account_id"] == "ANGEL_MAIN"


def test_get_assignment_for_unassigned_strategy_is_404(client, viewer_auth):
    r = client.get("/api/assignments/DoubleStraddelAlgo", headers=viewer_auth)
    assert r.status_code == 404


def test_assign_to_unknown_strategy_is_404(client, bearer):
    op = bearer(role="operator")
    r = client.post("/api/assignments", headers=op, json={"strategy_id": "NoSuchStrategy", "account_id": "ANGEL_MAIN"})
    assert r.status_code == 404


def test_assign_to_unknown_account_is_404(client, bearer):
    op = bearer(role="operator")
    r = client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "NO_SUCH_ACCOUNT"})
    assert r.status_code == 404


def test_assign_to_unavailable_broker_is_409(client, bearer):
    op = bearer(role="operator")
    r = client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "DHAN_MAIN"})
    assert r.status_code == 409


def test_assign_with_mismatched_execution_mode_is_422(client, bearer):
    op = bearer(role="operator")
    r = client.post("/api/assignments", headers=op,
                    json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN", "execution_mode": "LIVE"})
    assert r.status_code == 422


def test_assign_with_invalid_execution_mode_string_is_422(client, bearer):
    op = bearer(role="operator")
    r = client.post("/api/assignments", headers=op,
                    json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN", "execution_mode": "NOT_A_MODE"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# EXECUTION MODES
# --------------------------------------------------------------------------- #
def test_execution_modes_lists_all_four(client, viewer_auth):
    """Phase 14 added LIVE_CANARY as a distinct ExecutionMode member
    (trading/common/trading_account.py) -- this endpoint just enumerates
    the ExecutionMode enum as-is, so it must report all four values, not
    just the three that existed before Phase 14. This is a read-only
    listing endpoint: it grants no capability by itself. The actual
    safety gates (frontend LIVE_EXECUTION_ENABLED hiding LIVE/LIVE_CANARY
    from any selector, StrategyAssignment.assign()'s exact execution_mode
    match requirement, LiveCanaryGuard's fail-closed CanaryLimits, and the
    live_canary preflight command) are unaffected by what this list
    contains and are covered by their own dedicated tests."""
    r = client.get("/api/execution-modes", headers=viewer_auth)
    assert r.status_code == 200
    assert {m["value"] for m in r.json()} == {"LIVE", "LIVE_CANARY", "PAPER", "SHADOW"}


# --------------------------------------------------------------------------- #
# RISK
# --------------------------------------------------------------------------- #
def test_risk_status_defaults(client, viewer_auth):
    r = client.get("/api/risk/status", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["kill_switch"]["engaged"] is False
    assert body["limits"]["max_order_quantity"] is None
    assert body["assigned_strategy_count"] == 0


def test_risk_limits_all_unconfigured_by_default(client, viewer_auth):
    r = client.get("/api/risk/limits", headers=viewer_auth)
    assert r.status_code == 200
    assert all(v is None for v in r.json().values())


def test_kill_switch_requires_admin(client, bearer):
    op = bearer(role="operator")
    r = client.post("/api/risk/kill-switch", headers=op, json={"engaged": True, "reason": "test"})
    assert r.status_code == 403


def test_kill_switch_engage_and_disengage(client, admin_auth):
    r = client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": True, "reason": "manual halt"})
    assert r.status_code == 200
    body = r.json()
    assert body["engaged"] is True
    assert body["reason"] == "manual halt"
    assert body["engaged_by"].startswith("user:")
    assert body["engaged_at"] != ""

    r = client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": False})
    assert r.status_code == 200
    assert r.json()["engaged"] is False
    assert r.json()["disengaged_at"] != ""


def test_kill_switch_reflected_in_risk_status(client, admin_auth):
    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": True, "reason": "halt"})
    r = client.get("/api/risk/status", headers=admin_auth)
    assert r.json()["kill_switch"]["engaged"] is True


def test_kill_switch_reflected_in_system_status(client, admin_auth):
    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": True, "reason": "halt"})
    r = client.get("/api/system/status", headers=admin_auth)
    assert r.json()["kill_switch_engaged"] is True


# --------------------------------------------------------------------------- #
# ORDERS / POSITIONS / P&L -- honestly empty today (Phase 10 stub scope)
# --------------------------------------------------------------------------- #
def test_orders_positions_are_empty_today(client, viewer_auth):
    assert client.get("/api/execution/orders", headers=viewer_auth).json() == []
    assert client.get("/api/execution/positions", headers=viewer_auth).json() == []


def test_pnl_is_zeroed_today(client, viewer_auth):
    r = client.get("/api/execution/pnl", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["total_realized"] == 0.0
    assert body["total_unrealized"] == 0.0
    assert set(body["per_strategy"].keys()) == STRATEGY_IDS
    assert all(v == 0.0 for v in body["per_strategy"].values())


# --------------------------------------------------------------------------- #
# SYSTEM STATUS
# --------------------------------------------------------------------------- #
def test_system_status_reports_counts(client, bearer):
    op = bearer(role="operator")
    client.post("/api/strategies/CombinedVwapNifty/start", headers=op)
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})

    r = client.get("/api/system/status", headers=op)
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_counts_by_status"]["shadow"] == 1
    assert body["strategy_counts_by_status"]["disabled"] == 2
    assert body["account_count"] == 3
    assert body["broker_availability"]["angelone"] is True
    assert body["broker_availability"]["dhan"] is False
    assert body["assigned_strategy_count"] == 1


# --------------------------------------------------------------------------- #
# Audit logging
# --------------------------------------------------------------------------- #
def test_strategy_start_and_stop_are_audited(client, trader_auth, db_session):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    client.post("/api/strategies/CombinedVwapNifty/stop", headers=trader_auth)
    actions = [r.action for r in db_session.query(models.AuditLog).all()]
    assert "STRATEGY_STARTED" in actions
    assert "STRATEGY_STOPPED" in actions


def test_assignment_creation_is_audited(client, bearer, db_session):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    rows = [r for r in db_session.query(models.AuditLog).all() if r.action == "ASSIGNMENT_SET"]
    assert len(rows) == 1
    assert rows[0].outcome == "success"
    assert rows[0].target == "strategy:DoubleStraddelAlgo"


def test_kill_switch_actions_are_audited(client, admin_auth, db_session):
    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": True, "reason": "halt"})
    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": False})
    actions = [r.action for r in db_session.query(models.AuditLog).all()]
    assert "KILL_SWITCH_ENGAGED" in actions
    assert "KILL_SWITCH_DISENGAGED" in actions


def test_permission_denial_is_audited(client, viewer_auth, db_session):
    client.post("/api/strategies/CombinedVwapNifty/start", headers=viewer_auth)
    rows = [r for r in db_session.query(models.AuditLog).all() if r.action == "PERMISSION_DENIED"]
    assert len(rows) == 1
    assert rows[0].outcome == "denied"


# --------------------------------------------------------------------------- #
# Safety: no route ever reaches a real broker or the real accounts' data
# --------------------------------------------------------------------------- #
def test_execution_routes_module_never_calls_get_broker():
    import trading.api.execution_routes as mod

    source = open(mod.__file__, encoding="utf-8").read()
    assert "broker_manager.get_broker(" not in source
    assert ".connect()" not in source
    assert "place_order" not in source


def test_full_regression_isolation_between_tests(client, bearer):
    """Each test gets a fresh app (per tests/conftest.py's `app` fixture),
    so state from a previous test (e.g. a started strategy) must not leak
    into this one -- proves the Phase 11 ExecutionState is genuinely
    per-app, not a module-level singleton."""
    op = bearer(role="operator")
    r = client.get("/api/strategies/CombinedVwapNifty", headers=op)
    assert r.json()["status"] == "disabled"
