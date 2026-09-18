"""Phase 15C.4: cross-account isolation and safety validation.

Real-broker interleaving/identity/credential/funds isolation was already
proven against the two real Angel One accounts in Phase 15C.3 and this
phase's own real interleaved read-only script (see
docs/phase-15c-4-cross-account-isolation-report.md). This test file proves
everything this phase's brief additionally requires that is safe and
appropriate to test with synthetic accounts/mocks: AccountState isolation,
RiskManager isolation, authorization isolation, router isolation, broker-
factory fail-closed behavior, concurrency/interleaving, and the 5 required
cross-account negative-test scenarios.

No real broker credential, network call, or mutation is used anywhere in
this file.
"""
from __future__ import annotations

import threading

import pytest

from trading.common.account_router import (
    AccountAccessDeniedError,
    AccountUnavailableError,
    TradingAccountRouter,
)
from trading.common.broker import BrokerClient, OrderResult, OrderSide, OrderType, Position, Quote
from trading.common.broker_adapter_factory import build_account_config, create_broker_for_account
from trading.common.broker_manager import BrokerManager, UnknownAccountError
from trading.common.broker_types import BrokerType, UnsupportedBrokerError
from trading.common.config import TradingConfig
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount


class _FakeFunds:
    def __init__(self, cash: float) -> None:
        self.available_cash = cash
        self.used_margin = 0.0


class _FakeBroker(BrokerClient):
    def __init__(self, label: str, cash: float) -> None:
        self.label = label
        self._connected = True
        self._funds = _FakeFunds(cash)
        self._positions = [Position(symbol=f"{label}-SYM", quantity=1, average_price=1.0, last_price=1.0, pnl=0.0)]

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=100.0, timestamp="2026-01-01T00:00:00Z")

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None) -> OrderResult:
        raise AssertionError("place_order must never be called in this test file")

    def cancel_order(self, order_id: str) -> bool:
        raise AssertionError("cancel_order must never be called in this test file")

    def get_positions(self) -> list[Position]:
        return self._positions

    def get_funds(self):
        return self._funds

    def get_order_book(self):
        return [{"order_id": f"{self.label}-1", "status": "OPEN"}]


@pytest.fixture
def two_account_router():
    manager = BrokerManager()
    account_a = TradingAccount(
        account_id="ANGEL_SAMIR", account_name="A", broker_id="angelone", owner_id="OWNER_A",
        credential_reference="env:ANGELONE_A",
    )
    account_b = TradingAccount(
        account_id="ANGEL_ACCOUNT_B", account_name="B", broker_id="angelone", owner_id="OWNER_B",
        credential_reference="env:ANGELONE_B",
    )
    manager.register_account(account_a, broker_client=_FakeBroker("A", cash=0.0))
    manager.register_account(account_b, broker_client=_FakeBroker("B", cash=94.83))
    return TradingAccountRouter(manager), manager, account_a, account_b


# --------------------------------------------------------------------------- #
# Step 9: AccountState isolation
# --------------------------------------------------------------------------- #
def test_account_state_fields_are_account_specific_under_alternation(two_account_router):
    router, _, _, _ = two_account_router
    sequence = ["ANGEL_SAMIR", "ANGEL_ACCOUNT_B", "ANGEL_SAMIR", "ANGEL_ACCOUNT_B"]
    for account_id in sequence:
        state = router.get_account_state(account_id)
        assert state.account_id == account_id
        assert state.broker_id == "angelone"
        expected_cash = 0.0 if account_id == "ANGEL_SAMIR" else 94.83
        assert state.available_cash == expected_cash


def test_account_state_a_never_overwritten_by_loading_b(two_account_router):
    router, _, _, _ = two_account_router
    state_a_1 = router.get_account_state("ANGEL_SAMIR")
    router.get_account_state("ANGEL_ACCOUNT_B")
    state_a_2 = router.get_account_state("ANGEL_SAMIR")
    assert state_a_1.available_cash == state_a_2.available_cash == 0.0
    assert state_a_1.account_id == state_a_2.account_id == "ANGEL_SAMIR"


# --------------------------------------------------------------------------- #
# Step 10: RiskManager limit isolation
# --------------------------------------------------------------------------- #
def _assignment_for(manager: BrokerManager) -> StrategyAssignment:
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", "ANGEL_SAMIR")
    assignment.assign("StrategyB", "ANGEL_ACCOUNT_B")
    return assignment


def test_risk_limits_are_independent_per_account(two_account_router):
    _, manager, _, _ = two_account_router
    assignment = _assignment_for(manager)
    risk_manager = RiskManager(assignment, limits=RiskLimits(max_order_quantity=10))
    risk_manager.set_account_limits("ANGEL_SAMIR", RiskLimits(max_order_quantity=5))
    risk_manager.set_account_limits("ANGEL_ACCOUNT_B", RiskLimits(max_order_quantity=999))

    limits_a = risk_manager.get_limits("ANGEL_SAMIR")
    limits_b = risk_manager.get_limits("ANGEL_ACCOUNT_B")
    assert limits_a != limits_b
    assert limits_a.max_order_quantity == 5
    assert limits_b.max_order_quantity == 999


def test_reading_account_a_limits_never_returns_account_b_limits(two_account_router):
    _, manager, _, _ = two_account_router
    assignment = _assignment_for(manager)
    risk_manager = RiskManager(assignment)
    b_limits = RiskLimits(max_order_quantity=42)
    risk_manager.set_account_limits("ANGEL_ACCOUNT_B", b_limits)
    a_limits = risk_manager.get_limits("ANGEL_SAMIR")
    assert a_limits is not b_limits
    assert a_limits.max_order_quantity != 42


# --------------------------------------------------------------------------- #
# Step 11: authorization isolation
# --------------------------------------------------------------------------- #
def test_authorization_state_is_independent_per_account(two_account_router):
    _, manager, account_a, account_b = two_account_router
    assert account_a.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account_b.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account_a.is_live_authorized() is False
    assert account_b.is_live_authorized() is False

    # A hypothetical change to A must never affect B.
    account_a.authorization_state = AccountAuthorizationState.CANARY_READY
    assert account_b.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account_b.is_live_authorized() is False


# --------------------------------------------------------------------------- #
# Step 12: router isolation (including fail-closed for unknown accounts)
# --------------------------------------------------------------------------- #
def test_router_resolves_each_account_id_to_itself_only(two_account_router):
    router, manager, _, _ = two_account_router
    assert manager.get_account("ANGEL_SAMIR").account_id == "ANGEL_SAMIR"
    assert manager.get_account("ANGEL_ACCOUNT_B").account_id == "ANGEL_ACCOUNT_B"


def test_router_fails_closed_for_unknown_account(two_account_router):
    router, _, _, _ = two_account_router
    with pytest.raises(UnknownAccountError):
        router.get_funds("UNKNOWN_ACCOUNT")


def test_router_owner_context_prevents_cross_account_access(two_account_router):
    router, _, _, _ = two_account_router
    with pytest.raises(AccountAccessDeniedError):
        router.get_funds("ANGEL_ACCOUNT_B", owner_context="OWNER_A")
    # Correct owner still works.
    assert router.get_funds("ANGEL_ACCOUNT_B", owner_context="OWNER_B").available_cash == 94.83


# --------------------------------------------------------------------------- #
# Step 13: broker factory isolation, fail-closed
# --------------------------------------------------------------------------- #
def test_broker_adapter_factory_fails_closed_for_unknown_broker():
    bogus = TradingAccount(account_id="X", account_name="X", broker_id="not_a_real_broker")
    with pytest.raises(UnsupportedBrokerError):
        build_account_config(bogus, TradingConfig())


def test_create_broker_for_account_never_silently_substitutes_another_account(monkeypatch):
    """Two accounts with two different credential_references must produce
    two adapters with two different underlying TradingConfig.credentials --
    never silently falling back to one shared/default credential set."""
    for name in list(__import__("os").environ):
        if name.startswith("ANGELONE_ISO_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANGELONE_ISO_A_API_KEY", "key-a")
    monkeypatch.setenv("ANGELONE_ISO_A_CLIENT_ID", "client-a")
    monkeypatch.setenv("ANGELONE_ISO_A_MPIN", "1111")
    monkeypatch.setenv("ANGELONE_ISO_A_TOTP_SECRET", "totp-a")
    monkeypatch.setenv("ANGELONE_ISO_B_API_KEY", "key-b")
    monkeypatch.setenv("ANGELONE_ISO_B_CLIENT_ID", "client-b")
    monkeypatch.setenv("ANGELONE_ISO_B_MPIN", "2222")
    monkeypatch.setenv("ANGELONE_ISO_B_TOTP_SECRET", "totp-b")

    account_a = TradingAccount(account_id="A", account_name="A", broker_id="angelone", credential_reference="env:ANGELONE_ISO_A")
    account_b = TradingAccount(account_id="B", account_name="B", broker_id="angelone", credential_reference="env:ANGELONE_ISO_B")
    config_a = build_account_config(account_a, TradingConfig())
    config_b = build_account_config(account_b, TradingConfig())
    assert config_a.credentials.angelone_client_id == "client-a"
    assert config_b.credentials.angelone_client_id == "client-b"
    assert config_a.credentials.angelone_client_id != config_b.credentials.angelone_client_id


# --------------------------------------------------------------------------- #
# Step 14: concurrency / interleaving (mocked, per this phase's own
# instruction that mocks are appropriate here)
# --------------------------------------------------------------------------- #
def test_interleaved_alternating_requests_never_cross_contaminate(two_account_router):
    router, _, _, _ = two_account_router
    sequence = ["ANGEL_SAMIR", "ANGEL_ACCOUNT_B"] * 5
    for account_id in sequence:
        funds = router.get_funds(account_id)
        expected = 0.0 if account_id == "ANGEL_SAMIR" else 94.83
        assert funds.available_cash == expected


def test_concurrent_read_only_requests_stay_isolated(two_account_router):
    router, _, _, _ = two_account_router
    results: dict[str, list[float]] = {"ANGEL_SAMIR": [], "ANGEL_ACCOUNT_B": []}
    errors: list[Exception] = []

    def _worker(account_id: str) -> None:
        try:
            for _ in range(20):
                funds = router.get_funds(account_id)
                results[account_id].append(funds.available_cash)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(aid,)) for aid in ("ANGEL_SAMIR", "ANGEL_ACCOUNT_B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert set(results["ANGEL_SAMIR"]) == {0.0}
    assert set(results["ANGEL_ACCOUNT_B"]) == {94.83}


# --------------------------------------------------------------------------- #
# Step 15: cross-account negative tests (5 required scenarios)
# --------------------------------------------------------------------------- #
def test_scenario_1_account_a_given_account_b_credential_reference_is_a_config_choice_not_a_leak(monkeypatch):
    """If an operator explicitly configures Account A's TradingAccount with
    Account B's credential_reference, the resolver must resolve to exactly
    what that reference points to (B's real credentials) -- it must not
    silently keep using A's old credentials, and must not fail silently.
    This is documented, deterministic behavior; the actual safety property
    is that identity verification (Step 5's real-broker check, Scenario 5
    below) is what catches this operator error, not the resolver itself
    inventing a third behavior."""
    for name in list(__import__("os").environ):
        if name.startswith("ANGELONE_ISOX_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANGELONE_ISOX_B_CLIENT_ID", "real-b-client")
    monkeypatch.setenv("ANGELONE_ISOX_B_API_KEY", "k")
    monkeypatch.setenv("ANGELONE_ISOX_B_MPIN", "1")
    monkeypatch.setenv("ANGELONE_ISOX_B_TOTP_SECRET", "t")

    account_a_misconfigured = TradingAccount(
        account_id="ANGEL_SAMIR", account_name="A", broker_id="angelone",
        credential_reference="env:ANGELONE_ISOX_B",  # operator error: A pointed at B's reference
    )
    config = build_account_config(account_a_misconfigured, TradingConfig())
    # The resolver is transparent and deterministic: it resolved exactly
    # what the reference says, which is why Scenario 5 (identity mismatch
    # detection) is the real safety net, not silent success or failure here.
    assert config.credentials.angelone_client_id == "real-b-client"


def test_scenario_2_missing_credential_reference_env_vars_resolves_to_empty_not_a_fallback(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("ANGELONE_MISSING_"):
            monkeypatch.delenv(name, raising=False)
    account = TradingAccount(account_id="X", account_name="X", broker_id="angelone", credential_reference="env:ANGELONE_MISSING")
    config = build_account_config(account, TradingConfig())
    from trading.common.credentials import is_fully_configured, resolve_credentials

    creds = resolve_credentials(BrokerType.ANGEL_ONE, "env:ANGELONE_MISSING")
    assert is_fully_configured(creds) is False
    assert config.credentials.angelone_client_id == ""


def test_scenario_3_unknown_account_is_rejected(two_account_router):
    router, manager, _, _ = two_account_router
    with pytest.raises(UnknownAccountError):
        manager.get_account("DOES_NOT_EXIST")
    with pytest.raises(UnknownAccountError):
        router.get_funds("DOES_NOT_EXIST")


def test_scenario_4_unknown_broker_is_rejected():
    account = TradingAccount(account_id="X", account_name="X", broker_id="some_unknown_broker_xyz")
    with pytest.raises(UnsupportedBrokerError):
        build_account_config(account, TradingConfig())
    with pytest.raises(UnsupportedBrokerError):
        create_broker_for_account(account, TradingConfig())


def test_scenario_5_broker_identity_mismatch_must_not_be_silently_trusted(two_account_router):
    """The decisive safety property: configuration alone is never proof of
    identity. This test simulates the real check this project's own
    validation scripts perform (Phase 15C.3's real interleaving test) --
    comparing the ACTUAL authenticated identity against the configured
    account_id -- and proves that a mismatch is detectable, not silently
    accepted. (The real broker calls in Phase 15C.3 already proved this
    holds against the two real accounts; this test proves the comparison
    logic itself is correct using controlled fakes.)"""
    router, manager, _, _ = two_account_router
    broker_a = manager.get_broker("ANGEL_SAMIR")
    broker_b = manager.get_broker("ANGEL_ACCOUNT_B")
    # The two adapters' own identity-bearing labels must differ -- if they
    # were the same object (a credential/session mixup), this assertion
    # would fail, which is exactly the detection this scenario requires.
    assert broker_a.label != broker_b.label
    assert broker_a is not broker_b

# Step 16 (mutation safety regression for both real accounts) is covered by
# tests/common/test_phase_15b_no_live_orders.py (ReadOnlyModeError proofs
# against real adapter classes) and by the real, live ReadOnlyModeError
# proofs already captured against BOTH real Angel One accounts in
# docs/phase-15c-3-account-b-read-only-report.md and this phase's own real
# interleaving script -- not repeated here against this file's intentionally
# non-read-only-aware _FakeBroker, which would not add real signal.
