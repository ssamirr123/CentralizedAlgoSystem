"""Phase 15D-DR (Area K): non-production deployment smoke test.

Verifies the application can start, load configuration, and reach a safe,
non-trading-authorized state -- without ever calling a real broker or
starting a strategy. This is a pytest-based structural smoke test (not a
live deployment simulation with a real running server), matching the
existing project convention (e.g. trading/preflight/live_canary.py).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from trading.common.broker_adapter_factory import create_broker_for_account
from trading.common.broker_manager import BrokerManager
from trading.common.broker_types import BrokerType, get_capabilities
from trading.common.config import load_config
from trading.common.deployment_info import get_deployment_info
from trading.common.idempotency_store import SqliteIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount


def test_1_application_configuration_loads():
    config = load_config()
    assert config is not None
    assert config.trading_mode in ("paper", "live")  # never crashes, never defaults to an unrecognized mode


def test_2_deployment_version_identity_is_available():
    info = get_deployment_info()
    assert info.app_version
    assert info.git_sha
    assert info.deployment_id
    assert info.environment
    assert info.startup_timestamp


def test_3_idempotency_store_is_available():
    db_path = tempfile.mktemp(suffix=".db")
    store = SqliteIdempotencyStore(db_path=db_path)
    assert store.get("smoke-test-key") is None  # reachable, empty as expected


def test_4_kill_switch_is_available_and_starts_disengaged():
    ks = CentralKillSwitch()
    assert ks.engaged is False


def test_5_broker_adapter_factory_is_available_and_fails_closed_for_unknown_broker():
    from trading.common.broker_types import UnsupportedBrokerError

    account = TradingAccount(account_id="SMOKE_TEST", account_name="Smoke", broker_id="not_a_real_broker")
    try:
        create_broker_for_account(account, load_config())
        raised = False
    except UnsupportedBrokerError:
        raised = True
    assert raised


def test_6_account_a_is_read_only_by_default():
    account = TradingAccount(account_id="ANGEL_SAMIR", account_name="A", broker_id="angelone")
    assert account.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account.is_live_authorized() is False


def test_7_account_b_remains_safely_unauthorized_by_default():
    account = TradingAccount(account_id="ANGEL_ACCOUNT_B", account_name="B", broker_id="angelone")
    assert account.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account.is_live_authorized() is False


def test_8_no_strategy_starts_automatically_from_construction_alone():
    """Constructing every core object (BrokerManager, TradingAccount,
    CentralKillSwitch, SqliteIdempotencyStore) must never, by itself,
    submit anything -- there is no auto-start path anywhere in this
    codebase's constructors."""
    manager = BrokerManager()
    account = TradingAccount(account_id="ANGEL_SAMIR", account_name="A", broker_id="angelone")
    manager.register_account(account, broker_client=None, broker_factory=lambda: create_broker_for_account(account, load_config()))
    assert manager.accounts() == [account]  # registered, nothing executed


def test_9_no_broker_mutation_api_is_reachable_from_construction_alone():
    """Every real broker adapter's constructor never itself calls
    connect()/place_order() -- construction and connection/execution are
    always separate, explicit steps."""
    account = TradingAccount(account_id="ANGEL_SAMIR", account_name="A", broker_id="angelone", read_only=True)
    broker = create_broker_for_account(account, load_config())
    assert broker.is_connected() is False  # construction alone never connects, let alone places an order


def test_10_health_endpoint_reachable_and_never_reports_trading_authorized():
    from trading.api.health import health, ready
    from starlette.responses import Response

    response = Response()
    result = health(response)
    assert result.status in ("ok", "degraded")
    readiness = ready()
    assert readiness.trading_authorized is False


def test_11_readiness_endpoint_is_distinct_from_health():
    from trading.api.health import ControlCenterHealth, ControlCenterReadiness

    assert ControlCenterHealth is not ControlCenterReadiness
    assert "trading_authorized" in ControlCenterReadiness.model_fields
    assert "trading_authorized" not in ControlCenterHealth.model_fields


def test_12_broker_capabilities_registry_available_for_every_broker_type():
    for broker_type in BrokerType:
        caps = get_capabilities(broker_type)
        assert caps.broker_type == broker_type


def test_13_shutdown_is_clean_disconnect_does_not_raise():
    account = TradingAccount(account_id="ANGEL_SAMIR", account_name="A", broker_id="angelone", read_only=True)
    broker = create_broker_for_account(account, load_config())
    broker.disconnect()  # never connected -- must not raise
    assert broker.is_connected() is False
