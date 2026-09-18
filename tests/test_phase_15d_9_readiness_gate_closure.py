"""Phase 15D.9: closing the two blockers identified by the READ+PLAN
gate (docs/phase-15d-9-final-readiness-plan.md):

  Blocker 1 -- legacy algo broker bypass (DoubleStraddelAlgo,
  CombinedVwapNifty, Vwap_Algo_Nifty_hedge place real orders outside the
  centralized safety chain).
  Blocker 2 -- production_guard.py checked ENVIRONMENT/ENV, but the real
  deployment sets APP_ENV, so the guard silently never fired in production.

No test in this file places a real broker order, connects to a real
broker, or grants a real live authorization.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trading.common.deployment_info import get_deployment_info, resolve_environment
from trading.common.kill_switch import CentralKillSwitch
from trading.common.legacy_algo_readiness import (
    LEGACY_ALGO_NAMES,
    LegacyAlgoState,
    all_acceptable_for_canary,
    check_all_legacy_algos,
    check_legacy_algo_state,
)
from trading.common.legacy_execution_guard import LegacyExecutionBlocked, assert_live_mutation_allowed
from trading.common.production_guard import (
    ProductionSafetyError,
    check_production_safety_paths,
    is_production_environment,
)
from trading.database import models

REPO_ROOT = Path(__file__).resolve().parents[1]


# ======================================================================== #
# Blocker 2 -- production guard environment resolution
# ======================================================================== #
def test_resolve_environment_prefers_app_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "development")  # APP_ENV must win
    monkeypatch.delenv("ENV", raising=False)
    assert resolve_environment() == "production"


def test_resolve_environment_falls_back_to_environment_then_env(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert resolve_environment() == "staging"

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("ENV", "qa")
    assert resolve_environment() == "qa"


def test_resolve_environment_defaults_to_development(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    assert resolve_environment() == "development"


def test_deployment_info_and_production_guard_use_the_same_function():
    """Structural regression: assert there is exactly ONE environment-
    resolution function, not two independently-drifting copies -- the
    exact defect that caused Blocker 2."""
    import trading.common.deployment_info as di
    import trading.common.production_guard as pg

    assert pg.resolve_environment is di.resolve_environment


# Case 1: APP_ENV production recognized
def test_case_01_app_env_production_recognized():
    assert is_production_environment("production")
    assert is_production_environment("PRODUCTION")
    assert is_production_environment("prod")


# Case 2/3: ENVIRONMENT / ENV production recognized (compatibility preserved)
def test_case_02_environment_var_production_recognized(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert is_production_environment() is True


def test_case_03_env_var_production_recognized(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("ENV", "production")
    assert is_production_environment() is True


# Case 4: missing kill-switch path blocks startup (via APP_ENV specifically)
def test_case_04_app_env_production_missing_kill_switch_path_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    with pytest.raises(ProductionSafetyError, match="KILL_SWITCH_PERSISTENCE_PATH"):
        check_production_safety_paths()


# Case 5: missing audit path blocks startup (via APP_ENV specifically)
def test_case_05_app_env_production_missing_audit_path_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(tmp_path / "ks.json"))
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    with pytest.raises(ProductionSafetyError, match="AUDIT_DB_PATH"):
        check_production_safety_paths()


# Case 6: both paths present allows startup (via APP_ENV specifically)
def test_case_06_app_env_production_both_paths_present_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(tmp_path / "ks.json"))
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    check_production_safety_paths()  # must not raise


# Case 7: non-production behavior preserved
def test_case_07_non_production_app_env_values_are_noop(monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    for value in ("development", "docker", "staging", "test", ""):
        monkeypatch.setenv("APP_ENV", value)
        check_production_safety_paths()  # must not raise for any of these


# Case 8: actual docker-compose.prod.yml environment behavior verified
def test_case_08_docker_compose_prod_style_configuration(tmp_path, monkeypatch):
    """Reproduces the EXACT configuration this phase's own read step found
    in trading/infrastructure/backend/docker-compose.prod.yml: APP_ENV set,
    ENVIRONMENT/ENV never set. Before the Blocker-2 fix, this configuration
    made the guard silently no-op even with both persistence paths unset
    -- this is the regression test proving that specific failure mode is
    now closed."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    with pytest.raises(ProductionSafetyError):
        check_production_safety_paths()

    # And the deployment_info startup banner now also correctly reports
    # "production" instead of silently falling back to "development" --
    # (informational field only, never a trading-authorization signal).
    get_deployment_info.cache_clear()
    try:
        info = get_deployment_info()
        assert info.environment == "production"
    finally:
        get_deployment_info.cache_clear()


def test_lifespan_startup_fails_with_app_env_production_and_no_paths(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    from fastapi.testclient import TestClient

    from trading.api.app import create_app

    app = create_app()
    with pytest.raises(ProductionSafetyError):
        with TestClient(app):
            pass


def test_lifespan_startup_succeeds_with_app_env_production_and_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(tmp_path / "ks.json"))
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    from fastapi.testclient import TestClient

    from trading.api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200


# ======================================================================== #
# Blocker 1 -- legacy algo kill-switch guard
# ======================================================================== #
def test_assert_live_mutation_allowed_passes_when_switch_disengaged(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    assert_live_mutation_allowed(strategy_id="TestStrategy")  # must not raise


def test_assert_live_mutation_allowed_passes_with_unconfigured_path(monkeypatch):
    """No KILL_SWITCH_PERSISTENCE_PATH in this legacy process's own
    environment -- preserves CentralKillSwitch's exact pre-existing
    default (in-memory-only, disengaged) rather than inventing a new,
    stricter default for an already-running legacy process."""
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    assert_live_mutation_allowed(strategy_id="TestStrategy")  # must not raise


def test_assert_live_mutation_allowed_blocks_when_switch_engaged(tmp_path, monkeypatch):
    ks_path = tmp_path / "ks.json"
    switch = CentralKillSwitch(persistence_path=str(ks_path))
    switch.engage(by="operator", reason="test block")
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(ks_path))

    with pytest.raises(LegacyExecutionBlocked, match="test block"):
        assert_live_mutation_allowed(strategy_id="TestStrategy")


def test_assert_live_mutation_allowed_reflects_live_engage_disengage(tmp_path, monkeypatch):
    """The guard reads FRESH on every call -- an operator engaging the
    switch via the control-center API takes effect on the very next
    legacy-algo order attempt, no restart required."""
    ks_path = tmp_path / "ks.json"
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(ks_path))

    assert_live_mutation_allowed(strategy_id="TestStrategy")  # disengaged initially

    switch = CentralKillSwitch(persistence_path=str(ks_path))
    switch.engage(by="operator", reason="emergency")
    with pytest.raises(LegacyExecutionBlocked):
        assert_live_mutation_allowed(strategy_id="TestStrategy")

    switch.disengage(by="operator")
    assert_live_mutation_allowed(strategy_id="TestStrategy")  # must not raise again


def test_assert_live_mutation_allowed_never_mutates_the_switch_file(tmp_path, monkeypatch):
    ks_path = tmp_path / "ks.json"
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(ks_path))
    assert_live_mutation_allowed(strategy_id="TestStrategy")
    assert not ks_path.exists()  # a disengaged, never-engaged switch never even creates the file


def _load_legacy_module_for_source_inspection(relative_path: str):
    """Loads a legacy algo file as a plain text read (NOT an executable
    import -- these files depend on sibling modules like websocket_feed/
    manager/make_data that aren't installed as part of this test
    environment) purely to prove, structurally, that the kill-switch
    guard call precedes every real placeOrder call in the file."""
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative_path,strategy_id", [
    ("trading/algos/DoubleStraddelAlgo/broker/orders.py", "DoubleStraddelAlgo"),
    ("trading/algos/CombinedVwapNifty/rest_func.py", "CombinedVwapNifty"),
    ("trading/algos/Vwap_Algo_Nifty_hedge/rest_func.py", "Vwap_Algo_Nifty_hedge"),
])
def test_case_09_11_every_real_place_order_call_is_preceded_by_the_guard(relative_path, strategy_id):
    """Structural regression (Step 6): fails if a future developer adds a
    new `config.objconn.placeOrder(...)` call site to one of these three
    files without also calling `assert_live_mutation_allowed()`
    immediately before it -- catching an accidentally-restored direct
    mutation path."""
    source = _load_legacy_module_for_source_inspection(relative_path)
    assert "from trading.common.legacy_execution_guard import assert_live_mutation_allowed" in source
    assert f'_STRATEGY_ID = "{strategy_id}"' in source

    lines = source.splitlines()
    place_order_line_indexes = [i for i, line in enumerate(lines) if "objconn.placeOrder(" in line]
    assert place_order_line_indexes, f"expected at least one real placeOrder call site in {relative_path}"
    for idx in place_order_line_indexes:
        # The immediately preceding non-blank line must be the guard call.
        prev = idx - 1
        while prev >= 0 and not lines[prev].strip():
            prev -= 1
        assert "assert_live_mutation_allowed(strategy_id=_STRATEGY_ID)" in lines[prev], (
            f"{relative_path}: placeOrder call at line {idx + 1} is not immediately preceded by the guard"
        )


def test_case_12_legitimate_centralized_execution_remains_functional(tmp_path):
    """The new StrategyExecutionEngine path is completely untouched by
    this phase's legacy-algo fix -- a full happy-path execution through
    the centralized chain still works exactly as Phase 15D.6/15D.7 proved."""
    from tests.common.test_phase_15d_6_live_authorization_workflow import RecordingFakeBroker
    from trading.common.audit_store import PersistentAuditTrail
    from trading.common.broker_manager import BrokerManager
    from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
    from trading.common.idempotency_store import SqliteIdempotencyStore
    from trading.common.live_authorization import SqliteLiveAuthorizationStore
    from trading.common.order_intent import OrderIntent, OrderSide, OrderType
    from trading.common.risk_manager import RiskContext, RiskManager
    from trading.common.strategy_assignment import StrategyAssignment
    from trading.common.trading_account import ExecutionMode, TradingAccount

    broker = RecordingFakeBroker()
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = TradingAccount(account_id="ACC_B", account_name="B", broker_id="angelone",
                              credential_reference="env:ANGELONE_B", execution_mode=ExecutionMode.PAPER)
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("S", "ACC_B", execution_mode=ExecutionMode.PAPER)
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store,
    )
    intent = OrderIntent(strategy_id="S", account_id="ACC_B", symbol="NIFTY", exchange="NFO",
                          side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET, idempotency_key="k1")
    result = engine.execute(intent, context=RiskContext(reference_price=100.0))
    assert result.success
    assert broker.mutation_call_count == 1


def test_case_13_risk_manager_cannot_be_bypassed_in_new_path():
    """Documentation-as-code: RiskManager.validate() is still called
    unconditionally inside execute() -- this phase added no code path
    that skips it for the new engine."""
    import inspect

    from trading.common import execution

    source = inspect.getsource(execution.StrategyExecutionEngine.execute)
    assert "self._risk_manager.validate(" in source


def test_case_14_live_authorization_cannot_be_bypassed_in_new_path():
    import inspect

    from trading.common import execution

    source = inspect.getsource(execution.StrategyExecutionEngine.execute)
    assert "self._live_authorization_store.try_consume(" in source


def test_case_15_kill_switch_cannot_be_bypassed_in_new_path():
    import inspect

    from trading.common import execution

    source = inspect.getsource(execution.StrategyExecutionEngine.execute)
    assert "self._central_kill_switch" in source
    assert source.index("self._central_kill_switch") < source.index("self._risk_manager.validate(")


def test_case_16_idempotency_cannot_be_bypassed_in_new_path():
    import inspect

    from trading.common import execution

    source = inspect.getsource(execution.StrategyExecutionEngine.execute)
    assert "self._idempotency_store.claim(" in source


# ======================================================================== #
# Operational pre-canary check (Step 7)
# ======================================================================== #
def _make_algo(db_session, name: str, status: str) -> models.Algo:
    server = models.Server(name=f"srv-{name}", ec2_instance_id="i-fake", region="ap-south-1")
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    algo = models.Algo(name=name, server_id=server.id, script_path="fake.py", status=status)
    db_session.add(algo)
    db_session.commit()
    db_session.refresh(algo)
    return algo


# Case 17: STOPPED accepted
def test_case_17_stopped_accepted(db_session):
    _make_algo(db_session, "DoubleStraddelAlgo", status="STOPPED")
    result = check_legacy_algo_state(db_session, "DoubleStraddelAlgo")
    assert result.state == LegacyAlgoState.STOPPED
    assert result.acceptable_for_canary


# Case 18: DRY_RUN accepted
def test_case_18_dry_run_accepted(db_session):
    _make_algo(db_session, "CombinedVwapNifty", status="RUNNING")
    result = check_legacy_algo_state(db_session, "CombinedVwapNifty", known_dry_run=True)
    assert result.state == LegacyAlgoState.DRY_RUN
    assert result.acceptable_for_canary


# Case 19: RUNNING blocks
def test_case_19_running_blocks(db_session):
    _make_algo(db_session, "Vwap_Algo_Nifty_hedge", status="RUNNING")
    result = check_legacy_algo_state(db_session, "Vwap_Algo_Nifty_hedge")
    assert result.state == LegacyAlgoState.RUNNING
    assert not result.acceptable_for_canary


# Case 20: UNKNOWN blocks
def test_case_20_unknown_blocks_when_no_row_exists(db_session):
    result = check_legacy_algo_state(db_session, "NoSuchAlgo")
    assert result.state == LegacyAlgoState.UNKNOWN
    assert not result.acceptable_for_canary


def test_case_20b_unknown_blocks_on_query_failure():
    class _BrokenSession:
        def query(self, *a, **k):
            raise RuntimeError("db unavailable")

    result = check_legacy_algo_state(_BrokenSession(), "DoubleStraddelAlgo")
    assert result.state == LegacyAlgoState.UNKNOWN
    assert not result.acceptable_for_canary


def test_check_all_legacy_algos_covers_all_three_names(db_session):
    for name in LEGACY_ALGO_NAMES:
        _make_algo(db_session, name, status="STOPPED")
    results = check_all_legacy_algos(db_session)
    assert {r.name for r in results} == set(LEGACY_ALGO_NAMES)
    assert all_acceptable_for_canary(results)


def test_all_acceptable_for_canary_false_if_any_running(db_session):
    _make_algo(db_session, "DoubleStraddelAlgo", status="STOPPED")
    _make_algo(db_session, "CombinedVwapNifty", status="RUNNING")
    _make_algo(db_session, "Vwap_Algo_Nifty_hedge", status="STOPPED")
    results = check_all_legacy_algos(db_session)
    assert not all_acceptable_for_canary(results)


# Case 21: check is read-only
def test_case_21_check_is_read_only(db_session):
    algo = _make_algo(db_session, "DoubleStraddelAlgo", status="RUNNING")
    before_status = algo.status
    check_legacy_algo_state(db_session, "DoubleStraddelAlgo")
    db_session.refresh(algo)
    assert algo.status == before_status  # unchanged -- the check wrote nothing


# Case 22: check does not terminate processes
def test_case_22_module_has_no_process_termination_capability():
    """Documentation-as-code: the module never imports subprocess/os.kill/
    signal or any process-control primitive."""
    import trading.common.legacy_algo_readiness as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.kill", "signal.", "systemctl", "SIGTERM", "SIGKILL"):
        assert forbidden not in source


# ======================================================================== #
# mutation_call_count == 0 for the entire suite (explicit meta-check)
# ======================================================================== #
def test_no_real_broker_infrastructure_touched_in_this_file():
    """Documentation-as-code: this file never imports a real broker SDK
    or a real connect()-capable adapter directly -- RecordingFakeBroker
    (used in test_case_12) is imported from Phase 15D.6's own test
    module. Checks only actual import statements, not prose, to avoid a
    self-referential false positive from this very docstring."""
    import_lines = [
        line for line in Path(__file__).read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    forbidden_terms = ("smart_api", "smartconnect", "kiteconnect", "breeze_connect")
    for line in import_lines:
        for forbidden in forbidden_terms:
            assert forbidden not in line.lower()
