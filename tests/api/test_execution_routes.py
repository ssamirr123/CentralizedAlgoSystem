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
# ASSIGNMENTS -- duplicate guard (Phase 16.2)
# --------------------------------------------------------------------------- #
def test_create_duplicate_assignment_is_409(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    r = client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    assert r.status_code == 409
    assert "already assigned" in r.json()["detail"]


def test_create_assignment_with_replace_true_reassigns_without_error(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    r = client.post("/api/assignments", headers=op,
                     json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN", "replace": True})
    assert r.status_code == 201


# --------------------------------------------------------------------------- #
# ASSIGNMENTS -- readiness (Phase 16.2, read-only)
# --------------------------------------------------------------------------- #
def test_assignment_readiness_for_unassigned_strategy_is_404(client, viewer_auth):
    r = client.get("/api/assignments/DoubleStraddelAlgo/readiness", headers=viewer_auth)
    assert r.status_code == 404


def test_assignment_readiness_for_unknown_strategy_is_404(client, viewer_auth):
    r = client.get("/api/assignments/NoSuchStrategy/readiness", headers=viewer_auth)
    assert r.status_code == 404


def test_assignment_readiness_blocks_on_strategy_not_running(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    r = client.get("/api/assignments/DoubleStraddelAlgo/readiness", headers=op)
    assert r.status_code == 200
    body = r.json()
    assert body["order_execution_allowed"] is False
    assert body["strategy_status"] == "disabled"
    assert any("not running/shadow" in reason for reason in body["blocking_reasons"])


def test_assignment_readiness_reports_true_once_healthy(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategies/DoubleStraddelAlgo/start", headers=op)
    r = client.get("/api/assignments/DoubleStraddelAlgo/readiness", headers=op)
    body = r.json()
    assert body["order_execution_allowed"] is True
    assert body["blocking_reasons"] == []
    assert body["authorization_ok"] is True  # SHADOW mode -- default READ_ONLY is sufficient
    assert body["broker_capabilities"]["broker_type"] == "ANGEL_ONE"


def test_assignment_readiness_never_calls_a_broker_or_starts_a_strategy(client, bearer):
    """Purely diagnostic: hitting readiness must not itself change strategy
    status or account state (no side effects from a read)."""
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.get("/api/assignments/DoubleStraddelAlgo/readiness", headers=op)
    strategy = client.get("/api/strategies/DoubleStraddelAlgo", headers=op).json()
    assert strategy["status"] == "disabled"  # unchanged by the read-only readiness check


# --------------------------------------------------------------------------- #
# ACCOUNTS -- authorization_state exposure (Phase 16.2)
# --------------------------------------------------------------------------- #
def test_account_out_exposes_authorization_state(client, viewer_auth):
    r = client.get("/api/accounts/ANGEL_MAIN", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json()["authorization_state"] == "READ_ONLY"


# --------------------------------------------------------------------------- #
# STRATEGY LIFECYCLE (Phase 16.3, read-only)
# --------------------------------------------------------------------------- #
def test_strategy_lifecycle_list_covers_all_strategies(client, viewer_auth):
    r = client.get("/api/strategy-lifecycle", headers=viewer_auth)
    assert r.status_code == 200
    ids = {row["strategy_id"] for row in r.json()}
    assert ids == STRATEGY_IDS
    by_id = {row["strategy_id"]: row for row in r.json()}
    for row in r.json():
        assert row["lifecycle_state"] == "STOPPED"
        assert row["assignment_exists"] is False
        assert row["live_authorized"] is False
        assert row["last_market_data_at"] == ""
    # market_data_status only reflects the LAST time the runtime actually
    # attempted an evaluation cycle (run_once()) -- this endpoint never
    # calls run_once(), and none of these strategies have been started,
    # so every strategy (including Phase 16.7's CombinedVwapNifty, which
    # now declares required_instruments()) correctly reports "" here:
    # nothing has been evaluated yet, never fabricated.
    for strategy_id in STRATEGY_IDS:
        assert by_id[strategy_id]["market_data_status"] == ""


def test_strategy_lifecycle_detail_for_unknown_strategy_is_404(client, viewer_auth):
    r = client.get("/api/strategy-lifecycle/NoSuchStrategy", headers=viewer_auth)
    assert r.status_code == 404


def test_strategy_lifecycle_detail_reflects_assignment_before_enable_is_stopped(client, bearer):
    """The Control Center API has no standalone enable-only action --
    POST /api/strategies/{id}/start enables AND starts in one call (Phase
    11). A freshly-assigned-but-never-started strategy is therefore still
    StrategyStatus.DISABLED, which is lifecycle STOPPED, not READY. READY
    (StrategyStatus.ENABLED) is exercised directly at the Python level in
    tests/common/test_strategy_lifecycle.py; it is a real, tested state
    that is simply not independently reachable through this API today."""
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    r = client.get("/api/strategy-lifecycle/DoubleStraddelAlgo", headers=op)
    assert r.status_code == 200
    body = r.json()
    assert body["lifecycle_state"] == "STOPPED"
    assert body["strategy_status"] == "disabled"
    assert body["account_id"] == "ANGEL_MAIN"
    assert body["assignment_id"] == "DoubleStraddelAlgo"
    assert body["account_authorization_state"] == "READ_ONLY"
    assert body["live_authorized"] is False


def test_strategy_lifecycle_detail_reflects_running_after_start(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategies/DoubleStraddelAlgo/start", headers=op)
    r = client.get("/api/strategy-lifecycle/DoubleStraddelAlgo", headers=op)
    body = r.json()
    assert body["lifecycle_state"] == "RUNNING"
    assert body["execution_active"] is True


def test_strategy_lifecycle_reading_never_starts_a_strategy_or_mutates_anything(client, viewer_auth, bearer):
    op = bearer(role="operator")
    client.get("/api/strategy-lifecycle", headers=viewer_auth)
    client.get("/api/strategy-lifecycle/DoubleStraddelAlgo", headers=viewer_auth)
    strategy = client.get("/api/strategies/DoubleStraddelAlgo", headers=op).json()
    assignments = client.get("/api/assignments", headers=op).json()
    assert strategy["status"] == "disabled"
    assert assignments == []


# --------------------------------------------------------------------------- #
# STRATEGY CONTROL PLANE (Phase 16.4)
# --------------------------------------------------------------------------- #
def test_command_requires_start_permission_for_start(client, viewer_auth):
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=viewer_auth, json={"command": "START"})
    assert r.status_code == 403


def test_command_rejects_unknown_command_string(client, trader_auth):
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=trader_auth, json={"command": "GO_LIVE"})
    assert r.status_code == 422


def test_command_for_unknown_strategy_is_404(client, trader_auth):
    r = client.post("/api/strategy-lifecycle/NoSuchStrategy/command", headers=trader_auth, json={"command": "START"})
    assert r.status_code == 404


def test_start_command_rejected_with_no_assignment(client, trader_auth):
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=trader_auth, json={"command": "START"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "REJECTED"
    assert body["accepted"] is False
    assert body["execution_started"] is False


def test_start_command_accepted_once_assigned(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "ACCEPTED"
    assert body["new_state"] == "RUNNING"
    assert body["live_authorized"] is False
    assert body["execution_started"] is False


def test_repeated_start_command_is_idempotent_noop_not_409(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    assert r.status_code == 200  # never 409 -- idempotent, per this phase's own requirement
    assert r.json()["result"] == "NOOP"


def test_stop_command_noop_when_already_stopped(client, trader_auth):
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=trader_auth, json={"command": "STOP"})
    assert r.status_code == 200
    assert r.json()["result"] == "NOOP"


def test_stop_command_requires_stop_permission(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    viewer = bearer(role="viewer")
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=viewer, json={"command": "STOP"})
    assert r.status_code == 403


def test_command_start_then_stop_round_trip(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    r1 = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    assert r1.json()["new_state"] == "RUNNING"
    r2 = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "STOP"})
    assert r2.json()["new_state"] == "STOPPED"
    assert r2.json()["previous_state"] == "RUNNING"


def test_command_response_never_contains_a_credential_field(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    body_text = r.text.lower()
    for forbidden in ("api_key", "api_secret", "access_token", "password", "credential"):
        assert forbidden not in body_text


def test_command_is_audited(client, bearer, db_session):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    rows = [r for r in db_session.query(models.AuditLog).all() if r.action == "STRATEGY_COMMAND_ACCEPTED"]
    assert len(rows) == 1
    assert rows[0].outcome == "success"
    assert rows[0].target == "strategy:DoubleStraddelAlgo"


def test_pre_existing_start_stop_endpoints_are_unchanged_and_still_409_on_repeat(client, trader_auth):
    """Phase 16.4 must not alter the pre-existing Phase 11 contract of
    POST /api/strategies/{id}/start|stop -- only ADD the new control-plane
    endpoint above it."""
    r1 = client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    assert r1.status_code == 200
    r2 = client.post("/api/strategies/CombinedVwapNifty/start", headers=trader_auth)
    assert r2.status_code == 409


# --------------------------------------------------------------------------- #
# STRATEGY RUNTIME (Phase 16.5)
# --------------------------------------------------------------------------- #
def test_lifecycle_list_includes_runtime_fields(client, viewer_auth):
    r = client.get("/api/strategy-lifecycle", headers=viewer_auth)
    assert r.status_code == 200
    for row in r.json():
        assert row["runtime_state"] == "INACTIVE"
        assert row["last_cycle_at"] == ""
        assert row["last_result_summary"] == ""


def test_evaluate_unknown_strategy_is_404(client, trader_auth):
    r = client.post("/api/strategy-lifecycle/NoSuchStrategy/evaluate", headers=trader_auth)
    assert r.status_code == 404


def test_evaluate_requires_start_permission(client, viewer_auth):
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/evaluate", headers=viewer_auth)
    assert r.status_code == 403


def test_evaluate_inactive_strategy_is_a_pure_noop(client, trader_auth):
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/evaluate", headers=trader_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ticked"] is False
    assert body["intents_generated"] == 0
    assert body["executions"] == []


def test_evaluate_running_strategy_generates_zero_intents_by_design(client, bearer):
    """DoubleStraddelAlgo now has real, ported decision logic (Phase
    16.8), but build_execution_state() deliberately wires no
    market_data_source by default (avoiding any external/network
    dependency at app startup) -- evaluating it must fail closed to zero
    intents/executions, never fabricate activity."""
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/evaluate", headers=op)
    assert r.status_code == 200
    body = r.json()
    assert body["ticked"] is True
    assert body["intents_generated"] == 0
    assert body["error"] == ""

    lifecycle = client.get("/api/strategy-lifecycle/DoubleStraddelAlgo", headers=op).json()
    assert lifecycle["runtime_state"] == "HEALTHY"
    assert lifecycle["last_cycle_at"] != ""


def test_evaluate_combined_vwap_nifty_reports_no_data_with_no_market_data_source_configured(client, bearer):
    """Phase 16.7: CombinedVwapNifty now declares required_instruments()
    for its ported CE/PE legs. build_execution_state() deliberately wires
    no market_data_source by default (avoiding any external/network
    dependency at app startup) -- evaluating it must fail closed and
    report NO_DATA, never fabricate a signal."""
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "CombinedVwapNifty", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategy-lifecycle/CombinedVwapNifty/command", headers=op, json={"command": "START"})
    r = client.post("/api/strategy-lifecycle/CombinedVwapNifty/evaluate", headers=op)
    assert r.status_code == 200
    body = r.json()
    assert body["ticked"] is True
    assert body["intents_generated"] == 0

    lifecycle = client.get("/api/strategy-lifecycle/CombinedVwapNifty", headers=op).json()
    assert lifecycle["market_data_status"] == "NO_DATA"
    assert lifecycle["runtime_state"] == "HEALTHY"  # missing data is not a runtime failure


def test_evaluate_response_never_contains_a_credential_field(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    r = client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/evaluate", headers=op)
    body_text = r.text.lower()
    for forbidden in ("api_key", "api_secret", "access_token", "password", "credential"):
        assert forbidden not in body_text


def test_evaluate_is_audited(client, bearer, db_session):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/evaluate", headers=op)
    rows = [r for r in db_session.query(models.AuditLog).all() if r.action == "STRATEGY_RUNTIME_EVALUATED"]
    assert len(rows) == 1
    assert rows[0].outcome == "success"


# --------------------------------------------------------------------------- #
# WORKERS (Phase 16.9, read-only)
# --------------------------------------------------------------------------- #
def test_list_workers_starts_empty(client, viewer_auth):
    r = client.get("/api/workers", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json() == []


def test_get_unknown_worker_is_404(client, viewer_auth):
    r = client.get("/api/workers/no-such-worker", headers=viewer_auth)
    assert r.status_code == 404


def test_get_unknown_worker_strategies_is_404(client, viewer_auth):
    r = client.get("/api/workers/no-such-worker/strategies", headers=viewer_auth)
    assert r.status_code == 404


def test_worker_endpoints_reflect_a_registered_worker(client, viewer_auth, app):
    app.state.execution.worker_registry.register_worker(worker_id="w1", name="Worker One", version="1.0")
    app.state.execution.worker_registry.assign_strategy("DoubleStraddelAlgo", "w1")

    r = client.get("/api/workers", headers=viewer_auth)
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["worker_id"] == "w1"
    assert r.json()[0]["status"] == "ONLINE"

    r2 = client.get("/api/workers/w1", headers=viewer_auth)
    assert r2.status_code == 200
    assert r2.json()["version"] == "1.0"

    r3 = client.get("/api/workers/w1/strategies", headers=viewer_auth)
    assert r3.status_code == 200
    assert r3.json() == ["DoubleStraddelAlgo"]


def test_worker_response_never_contains_a_credential_field(client, viewer_auth, app):
    app.state.execution.worker_registry.register_worker(worker_id="w1", name="Worker One")
    r = client.get("/api/workers", headers=viewer_auth)
    body_text = r.text.lower()
    for forbidden in ("api_key", "api_secret", "access_token", "password", "credential"):
        assert forbidden not in body_text


# --------------------------------------------------------------------------- #
# WORKER ASSIGNMENT (Phase 16.12)
# --------------------------------------------------------------------------- #
def test_assign_worker_strategy_requires_trading_control(client, bearer, app):
    app.state.execution.worker_registry.register_worker(worker_id="w1", name="A")
    trader = bearer(role="trader")
    r = client.post("/api/workers/w1/assign", headers=trader, json={"strategy_id": "DoubleStraddelAlgo"})
    assert r.status_code == 403


def test_assign_worker_strategy_succeeds_for_operator(client, bearer, app):
    app.state.execution.worker_registry.register_worker(worker_id="w1", name="A")
    op = bearer(role="operator")
    r = client.post("/api/workers/w1/assign", headers=op, json={"strategy_id": "DoubleStraddelAlgo"})
    assert r.status_code == 200
    assert r.json()["assigned_strategy_ids"] == ["DoubleStraddelAlgo"]


def test_assign_worker_strategy_unknown_worker_is_404(client, bearer):
    op = bearer(role="operator")
    r = client.post("/api/workers/ghost/assign", headers=op, json={"strategy_id": "DoubleStraddelAlgo"})
    assert r.status_code == 404


def test_assign_worker_strategy_already_owned_by_online_worker_is_409(client, bearer, app):
    app.state.execution.worker_registry.register_worker(worker_id="w1", name="A")
    app.state.execution.worker_registry.register_worker(worker_id="w2", name="B")
    op = bearer(role="operator")
    r1 = client.post("/api/workers/w1/assign", headers=op, json={"strategy_id": "DoubleStraddelAlgo"})
    assert r1.status_code == 200
    r2 = client.post("/api/workers/w2/assign", headers=op, json={"strategy_id": "DoubleStraddelAlgo"})
    assert r2.status_code == 409


def test_assign_worker_strategy_is_audited(client, bearer, app, db_session):
    from trading.database import models

    app.state.execution.worker_registry.register_worker(worker_id="w1", name="A")
    op = bearer(role="operator")
    client.post("/api/workers/w1/assign", headers=op, json={"strategy_id": "DoubleStraddelAlgo"})
    rows = [r for r in db_session.query(models.AuditLog).all() if r.action == "STRATEGY_WORKER_ASSIGNED"]
    assert len(rows) == 1


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
# PORTFOLIO RISK (Phase 16.10, read-only)
# --------------------------------------------------------------------------- #
def test_portfolio_risk_starts_healthy_and_zeroed(client, viewer_auth):
    r = client.get("/api/risk/portfolio", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["risk_status"] == "HEALTHY"
    assert body["daily_pnl"] == 0.0
    assert body["gross_exposure"] == 0.0
    assert body["open_orders"] == 0
    assert body["orders_today"] == 0
    assert body["limits"]["max_portfolio_exposure"] is None


def test_list_account_risk_covers_every_account(client, viewer_auth):
    r = client.get("/api/risk/accounts", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert {row["account_id"] for row in body} == {"ANGEL_MAIN", "DHAN_MAIN", "ICICI_MAIN"}
    assert all(row["risk_status"] == "HEALTHY" for row in body)


def test_get_account_risk_for_unknown_account_is_404(client, viewer_auth):
    r = client.get("/api/risk/accounts/NO_SUCH_ACCOUNT", headers=viewer_auth)
    assert r.status_code == 404


def test_list_strategy_risk_covers_every_strategy(client, viewer_auth):
    r = client.get("/api/risk/strategies", headers=viewer_auth)
    assert r.status_code == 200
    assert {row["strategy_id"] for row in r.json()} == STRATEGY_IDS


def test_get_strategy_risk_for_unknown_strategy_is_404(client, viewer_auth):
    r = client.get("/api/risk/strategies/NO_SUCH_STRATEGY", headers=viewer_auth)
    assert r.status_code == 404


def test_portfolio_risk_endpoints_never_expose_order_mutation_verbs(client, viewer_auth):
    for path in ("/api/risk/portfolio", "/api/risk/accounts", "/api/risk/strategies"):
        r = client.get(path, headers=viewer_auth)
        body_text = r.text.lower()
        for forbidden in ("place_order", "buy", "sell", "authorize_live", "go_live"):
            assert forbidden not in body_text


# --------------------------------------------------------------------------- #
# OPERATIONS (Phase 16.11, read-only)
# --------------------------------------------------------------------------- #
def test_operations_summary_requires_authentication(client):
    r = client.get("/api/operations/summary")
    assert r.status_code == 401


def test_operations_summary_reports_real_state(client, viewer_auth):
    r = client.get("/api/operations/summary", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["system"]["ready"] is True
    assert body["system"]["git_sha"] != ""
    assert set(s["strategy_id"] for s in body["strategies"]) == STRATEGY_IDS
    assert {a["account_id"] for a in body["accounts"]} == {"ANGEL_MAIN", "DHAN_MAIN", "ICICI_MAIN"}
    assert body["workers"] == []
    assert body["safety"]["execution_mode_banner"] == "SHADOW"
    assert body["safety"]["live_trading_disabled"] is True
    assert body["safety"]["kill_switch_engaged"] is False
    assert body["active_alerts"] == []


def test_operations_system_matches_summary(client, viewer_auth):
    r = client.get("/api/operations/system", headers=viewer_auth)
    assert r.status_code == 200
    assert r.json()["environment"] != ""


def test_operations_workers_reflects_registered_worker(client, viewer_auth, app):
    app.state.execution.worker_registry.register_worker(worker_id="w1", name="Worker One")
    r = client.get("/api/operations/workers", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["worker_id"] == "w1"
    assert body[0]["status"] == "ONLINE"
    assert body[0]["heartbeat_age_seconds"] is not None


def test_operations_strategies_shows_lifecycle_and_runtime_state(client, viewer_auth):
    r = client.get("/api/operations/strategies", headers=viewer_auth)
    assert r.status_code == 200
    body = {s["strategy_id"]: s for s in r.json()}
    assert set(body) == STRATEGY_IDS
    for s in body.values():
        assert s["lifecycle_state"] == "STOPPED"
        assert s["runtime_state"] == "INACTIVE"


def test_operations_accounts_never_exposes_credentials(client, viewer_auth):
    r = client.get("/api/operations/accounts", headers=viewer_auth)
    body_text = r.text.lower()
    for forbidden in ("api_key", "api_secret", "access_token", "password", "totp", "credential"):
        assert forbidden not in body_text


def test_kill_switch_engaged_raises_operations_alert(client, admin_auth, viewer_auth):
    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": True, "reason": "halt"})
    r = client.get("/api/operations/summary", headers=viewer_auth)
    codes = {a["code"] for a in r.json()["active_alerts"]}
    assert "KILL_SWITCH_ENGAGED" in codes
    assert r.json()["safety"]["kill_switch_engaged"] is True

    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": False})
    r2 = client.get("/api/operations/summary", headers=viewer_auth)
    codes2 = {a["code"] for a in r2.json()["active_alerts"]}
    assert "KILL_SWITCH_ENGAGED" not in codes2


def test_worker_offline_raises_and_resolves_alert(client, viewer_auth, app):
    registry = app.state.execution.worker_registry
    registry.register_worker(worker_id="w1", name="Worker One")
    registry.mark_offline("w1")

    r = client.get("/api/operations/alerts", headers=viewer_auth)
    codes = {a["code"] for a in r.json()}
    assert "WORKER_OFFLINE" in codes

    # Re-registering (Phase 16.9 restart semantics) brings a fresh session
    # back ONLINE -- the alert must resolve.
    registry.register_worker(worker_id="w1", name="Worker One")
    r2 = client.get("/api/operations/alerts", headers=viewer_auth)
    codes2 = {a["code"] for a in r2.json()}
    assert "WORKER_OFFLINE" not in codes2


def test_operations_alerts_filters_by_severity_and_category(client, viewer_auth, admin_auth):
    client.post("/api/risk/kill-switch", headers=admin_auth, json={"engaged": True, "reason": "halt"})
    r = client.get("/api/operations/alerts", headers=viewer_auth, params={"category": "SYSTEM"})
    assert all(a["category"] == "SYSTEM" for a in r.json())
    r2 = client.get("/api/operations/alerts", headers=viewer_auth, params={"severity": "CRITICAL"})
    assert all(a["severity"] == "CRITICAL" for a in r2.json())


def test_operations_audit_filters_by_strategy_id(client, bearer):
    op = bearer(role="operator")
    client.post("/api/assignments", headers=op, json={"strategy_id": "DoubleStraddelAlgo", "account_id": "ANGEL_MAIN"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/command", headers=op, json={"command": "START"})
    client.post("/api/strategy-lifecycle/DoubleStraddelAlgo/evaluate", headers=op)

    r = client.get("/api/operations/audit", headers=op, params={"strategy_id": "DoubleStraddelAlgo"})
    assert r.status_code == 200
    assert all(row["strategy_id"] == "DoubleStraddelAlgo" for row in r.json())

    r2 = client.get("/api/operations/audit", headers=op, params={"strategy_id": "NoSuchStrategy"})
    assert r2.json() == []


def test_operations_intents_and_executions_are_bounded_and_read_only(client, viewer_auth):
    r = client.get("/api/operations/intents", headers=viewer_auth, params={"limit": 5})
    assert r.status_code == 200
    assert len(r.json()) <= 5
    r2 = client.get("/api/operations/executions", headers=viewer_auth, params={"limit": 5})
    assert r2.status_code == 200
    assert len(r2.json()) <= 5


def test_operations_endpoints_never_expose_order_mutation_verbs(client, viewer_auth):
    for path in (
        "/api/operations/summary", "/api/operations/system", "/api/operations/workers",
        "/api/operations/strategies", "/api/operations/accounts", "/api/operations/alerts",
        "/api/operations/audit", "/api/operations/intents", "/api/operations/executions",
    ):
        r = client.get(path, headers=viewer_auth)
        body_text = r.text.lower()
        for forbidden in ("place_order", "modify_order", "cancel_order", "authorize_live", "go_live"):
            assert forbidden not in body_text


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


def test_execution_routes_module_never_touches_live_authorization():
    """Phase 16.1 (Control Center foundation validation): this control-
    center EXECUTION API layer (strategy/account/risk/assignment control)
    must never itself create or consume a LiveAuthorization, and must never
    construct a StrategyExecutionEngine -- Phase 17.1-R Remediation F
    deliberately wired LiveAuthorization into a SEPARATE, dedicated router
    (trading/api/live_authorization_routes.py, see that module's own
    docstring for the full authenticated-operator-identity design) rather
    than into this module, specifically so this architectural boundary
    stays meaningful: execution_routes.py's own routes remain exactly as
    LiveAuthorization-free as they were before that phase. Mirrors the
    existing test_execution_routes_module_never_calls_get_broker's own
    structural source-scan pattern."""
    import trading.api.execution_routes as mod

    source = open(mod.__file__, encoding="utf-8").read()
    assert "LiveAuthorization" not in source
    assert "try_consume(" not in source
    assert "StrategyExecutionEngine(" not in source


def test_live_authorization_routes_module_is_the_sole_owner_of_that_capability():
    """Phase 17.1-R companion to the guard above: proves the NEW
    live-authorization capability lives exclusively in its own dedicated
    router, never leaking into execution_routes.py, and that it is wired
    through the SAME Depends(require_permission(...)) RBAC every other
    route in this API already uses -- never a separate, weaker auth path."""
    import trading.api.live_authorization_routes as live_auth_mod

    source = open(live_auth_mod.__file__, encoding="utf-8").read()
    assert "require_permission" in source
    # never actually IMPORTS the worker-auth machine lane as a substitute
    # for operator identity -- checked against the module's real import
    # statements, not its own docstring prose (which explains this exact
    # design choice using these same words).
    import ast
    tree = ast.parse(source)
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "worker_auth" not in imported_names
    assert "WorkerAuthRegistry" not in imported_names


def test_execution_state_wires_live_authorization_only_via_dedicated_fields():
    """Read-only structural check on the actual constructed ExecutionState
    used by every route in this module -- not just its source text.

    Phase 17.1-R Remediation F deliberately added `idempotency_store`,
    `live_authorization_store`, and `authorization_service` to
    ExecutionState (see that module's own docstring for why) -- this test
    now asserts the FULL exact field set, so any FUTURE addition here is
    equally deliberate and equally reviewed, not silent creep. It no longer
    asserts absence, since presence is now the reviewed, intended state."""
    from trading.api.execution_state import ExecutionState, build_execution_state

    state = build_execution_state()
    assert hasattr(state, "live_authorization_store")
    assert set(ExecutionState.__dataclass_fields__) == {
        "broker_manager", "strategy_assignment", "risk_manager", "strategy_registry",
        "kill_switch", "metrics", "audit_trail", "alerts", "strategy_runtime",
        "worker_registry", "worker_coordinator", "portfolio_risk_manager", "operational_alerts",
        "worker_auth_registry", "idempotency_store", "live_authorization_store", "authorization_service",
        "reconciliation_service",
    }
    # Phase 16.5's own runtime is present, but it is not a
    # LiveAuthorization store -- it holds no live-authorization state of
    # any kind (see trading/common/strategy_runtime.py's own docstring).
    assert not hasattr(state.strategy_runtime, "live_authorization_store")
    # Phase 16.9: the worker registry starts with zero workers -- nothing
    # is ever auto-registered.
    assert state.worker_registry.list_workers() == []


def test_full_regression_isolation_between_tests(client, bearer):
    """Each test gets a fresh app (per tests/conftest.py's `app` fixture),
    so state from a previous test (e.g. a started strategy) must not leak
    into this one -- proves the Phase 11 ExecutionState is genuinely
    per-app, not a module-level singleton."""
    op = bearer(role="operator")
    r = client.get("/api/strategies/CombinedVwapNifty", headers=op)
    assert r.json()["status"] == "disabled"
