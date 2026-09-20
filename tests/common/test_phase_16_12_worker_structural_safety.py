"""Phase 16.12 Section 42/50: structural proof that the worker application
(trading/worker/*) cannot reach a real broker, cannot self-authorize, and
holds no central risk/execution authority."""
from __future__ import annotations

import inspect

import trading.worker.client as client_module
import trading.worker.config as config_module
import trading.worker.main as main_module
import trading.worker.runner as runner_module

_WORKER_MODULES = (client_module, config_module, main_module, runner_module)

_FORBIDDEN_TOKENS = (
    "AngelOneBroker", "angelone", "DhanBroker", "dhan_broker",
    "ICICIBreezeBroker", "icici_breeze_broker", "SmartConnect",
    "place_order(", "placeOrder", "modify_order(", "modifyOrder",
    "cancel_order(", "cancelOrder",
)

_FORBIDDEN_IMPORTS = (
    "trading.common.brokers.angelone",
    "trading.common.brokers.dhan",
    "trading.common.brokers.icici_breeze",
    "trading.common.risk_manager",
    "trading.common.portfolio_risk",
    "trading.common.execution",
    "trading.common.worker_registry",
    "trading.common.worker_coordinator",
)


def test_worker_modules_never_import_a_broker_adapter_or_sdk():
    for module in _WORKER_MODULES:
        source = inspect.getsource(module)
        for forbidden in _FORBIDDEN_TOKENS:
            assert forbidden not in source, f"{module.__name__} contains forbidden token {forbidden!r}"


def test_worker_modules_never_import_central_risk_or_execution_authority():
    for module in _WORKER_MODULES:
        source = inspect.getsource(module)
        for forbidden in _FORBIDDEN_IMPORTS:
            assert forbidden not in source, f"{module.__name__} imports forbidden module {forbidden!r}"


def test_worker_modules_never_import_live_authorization():
    for module in _WORKER_MODULES:
        assert not hasattr(module, "LiveAuthorization")
        source = inspect.getsource(module)
        assert "live_authorization" not in source.lower()
        assert "LIVE_AUTHORIZED" not in source


def test_worker_config_never_reads_a_broker_trading_credential_env_var():
    source = inspect.getsource(config_module)
    for forbidden in ("API_SECRET", "BROKER_PASSWORD", "TOTP_SECRET", "ACCESS_TOKEN", "MPIN"):
        assert forbidden not in source


def test_tcc_client_only_ever_calls_the_worker_machine_api():
    """The worker-side HTTP client must only ever call /api/worker/* --
    never a strategy-control, risk, or admin endpoint directly."""
    source = inspect.getsource(client_module)
    for path in ("/api/strategies", "/api/risk", "/api/admin", "/api/assignments", "/api/strategy-lifecycle"):
        assert path not in source
    assert "/api/worker/" in source
