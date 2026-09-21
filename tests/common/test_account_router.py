"""Phase 15B section 10/17: TradingAccountRouter -- independent resolution
per account_id, and owner-context isolation, using two fake in-memory
accounts (no real broker credentials needed)."""
from __future__ import annotations

import pytest

from trading.common.account_router import (
    AccountAccessDeniedError,
    AccountUnavailableError,
    TradingAccountRouter,
)
from trading.common.broker import BrokerClient, OrderResult, OrderSide, OrderType, Position, Quote
from trading.common.broker_manager import BrokerManager
from trading.common.trading_account import AccountAuthorizationState, TradingAccount


class _FakeFunds:
    def __init__(self, cash: float) -> None:
        self.available_cash = cash
        self.used_margin = 0.0


class _FakeBroker(BrokerClient):
    """A minimal, fully in-memory double -- carries a distinct `label` so a
    test can prove which credentials/account a resolved broker actually
    belongs to."""

    def __init__(self, label: str, cash: float) -> None:
        self.label = label
        self._connected = True
        self._funds = _FakeFunds(cash)
        self._positions = [Position(symbol="NIFTY", quantity=0, average_price=0.0, last_price=0.0, pnl=0.0)]

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=100.0, timestamp="2026-01-01T00:00:00Z")

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None) -> OrderResult:
        raise AssertionError("place_order must never be called via TradingAccountRouter")

    def cancel_order(self, order_id: str) -> bool:
        raise AssertionError("cancel_order must never be called via TradingAccountRouter")

    def get_positions(self) -> list[Position]:
        return self._positions

    def get_funds(self):
        return self._funds

    def get_order_book(self):
        return []


@pytest.fixture
def router() -> tuple[TradingAccountRouter, BrokerManager]:
    manager = BrokerManager()
    samir = TradingAccount(
        account_id="ANGEL_SAMIR", account_name="Samir Angel", broker_id="angelone", owner_id="SAMIR",
    )
    wife = TradingAccount(
        account_id="ANGEL_WIFE", account_name="Wife Angel", broker_id="angelone", owner_id="WIFE",
    )
    manager.register_account(samir, broker_client=_FakeBroker("samir", cash=1000.0))
    manager.register_account(wife, broker_client=_FakeBroker("wife", cash=2000.0))
    return TradingAccountRouter(manager), manager


def test_get_funds_resolves_independently_per_account(router):
    account_router, _ = router
    samir_funds = account_router.get_funds("ANGEL_SAMIR")
    wife_funds = account_router.get_funds("ANGEL_WIFE")
    assert samir_funds.available_cash == 1000.0
    assert wife_funds.available_cash == 2000.0


def test_owner_context_cannot_reach_a_different_owners_account(router):
    account_router, _ = router
    with pytest.raises(AccountAccessDeniedError):
        account_router.get_funds("ANGEL_WIFE", owner_context="SAMIR")
    # But the correct owner_context still works.
    assert account_router.get_funds("ANGEL_WIFE", owner_context="WIFE").available_cash == 2000.0


def test_no_owner_context_is_allowed_for_system_level_callers(router):
    account_router, _ = router
    # owner_context=None (the default) is the explicit "no scoping" case.
    assert account_router.get_funds("ANGEL_SAMIR").available_cash == 1000.0


def test_disabled_account_is_unavailable_even_for_reads(router):
    account_router, manager = router
    account = manager.get_account("ANGEL_SAMIR")
    account.authorization_state = AccountAuthorizationState.DISABLED
    with pytest.raises(AccountUnavailableError):
        account_router.get_funds("ANGEL_SAMIR")


def test_killed_account_is_unavailable_and_cannot_be_revived(router):
    account_router, manager = router
    account = manager.get_account("ANGEL_SAMIR")
    account.set_killed(reason="test")
    with pytest.raises(AccountUnavailableError):
        account_router.get_funds("ANGEL_SAMIR")
    # No un-kill path exists.
    assert not hasattr(account, "revive") and not hasattr(account, "un_kill")


def test_get_account_state_normalizes_without_placing_any_order(router):
    account_router, _ = router
    state = account_router.get_account_state("ANGEL_SAMIR")
    assert state.account_id == "ANGEL_SAMIR"
    assert state.broker_id == "angelone"
    assert state.available_cash == 1000.0
    assert state.position_count == 1
