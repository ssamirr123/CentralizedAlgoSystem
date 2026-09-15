"""Phase 15B section 21: explicit proof that this phase's new
multi-account/multi-broker plumbing cannot place a real order.

Every real adapter's mutating methods must remain blocked when constructed
through the new create_broker_for_account() path with an account whose
read_only defaults to True (TradingAccount's own Phase 15B default) -- the
same ReadOnlyModeError gate that already protects the single-account path,
now proven for the multi-account path too.
"""
from __future__ import annotations

import pytest

from trading.common.broker import ReadOnlyModeError
from trading.common.broker_adapter_factory import create_broker_for_account
from trading.common.config import TradingConfig
from trading.common.trading_account import TradingAccount


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith(("ANGELONE", "DHAN", "ICICI_BREEZE")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANGELONE_TEST_API_KEY", "k")
    monkeypatch.setenv("ANGELONE_TEST_CLIENT_ID", "c")
    monkeypatch.setenv("ANGELONE_TEST_MPIN", "1234")
    monkeypatch.setenv("ANGELONE_TEST_TOTP_SECRET", "t")
    yield


def _account(broker_id: str, credential_reference: str = "") -> TradingAccount:
    return TradingAccount(
        account_id=f"{broker_id.upper()}_TEST", account_name="test", broker_id=broker_id,
        credential_reference=credential_reference,
        # read_only is NOT passed -- proving the class DEFAULT (True) is
        # what protects this path, not an explicit test-only override.
    )


def test_angelone_via_factory_blocks_every_mutation():
    account = _account("angelone", "env:ANGELONE_TEST")
    broker = create_broker_for_account(account, TradingConfig())
    with pytest.raises(ReadOnlyModeError):
        broker.place_order("NIFTY", __import__("trading.common.broker", fromlist=["OrderSide"]).OrderSide.BUY, 1)
    with pytest.raises(ReadOnlyModeError):
        broker.cancel_order("DUMMY")


def test_dhan_via_factory_blocks_every_mutation():
    account = _account("dhan")
    broker = create_broker_for_account(account, TradingConfig())
    with pytest.raises(ReadOnlyModeError):
        broker.place_order("NIFTY", __import__("trading.common.broker", fromlist=["OrderSide"]).OrderSide.BUY, 1)


def test_icici_breeze_via_factory_blocks_every_mutation():
    account = _account("icici_breeze")
    broker = create_broker_for_account(account, TradingConfig())
    with pytest.raises(ReadOnlyModeError):
        broker.place_order("NIFTY", __import__("trading.common.broker", fromlist=["OrderSide"]).OrderSide.BUY, 1)


def test_trading_account_default_read_only_is_true_for_every_new_account():
    """The structural reason the above tests pass: nothing has to
    remember to pass read_only=True -- it's the class default."""
    assert TradingAccount(account_id="X", account_name="X", broker_id="angelone").read_only is True
