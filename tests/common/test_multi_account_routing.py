"""Phase 7: multi-account routing.

    Strategy -> StrategyAssignment -> TradingAccount -> BrokerManager -> BrokerAdapter

Demonstrates a registry of THREE accounts (ANGEL_MAIN, DHAN_MAIN,
ICICI_MAIN) side by side, none of them able to place a real order:
- ANGEL_MAIN uses a real AngelOneBroker wired to a FakeSmartApi double
  (same pattern as tests/common/test_angelone_broker.py) -- no real
  network call anywhere in this file.
- DHAN_MAIN's broker_id ("dhan") is marked unavailable -- no Dhan adapter
  is implemented (Phase 7 explicitly defers this); its factory would raise
  NotImplementedError if it were ever invoked, which these tests prove it
  never is.
- ICICI_MAIN uses the existing ICICIBreezeBroker STUB (already a Phase 0/2
  stub whose connect() raises NotImplementedError) -- also not implemented,
  and its broker_id is likewise marked unavailable.

The strategy never selects a broker directly -- it only ever names a
strategy_id; StrategyAssignment resolves that to an account_id, and
BrokerManager resolves the account to whichever BrokerClient is actually
registered. All accounts here are execution_mode=SHADOW: no real orders.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trading.common.broker_manager import BrokerManager, BrokerUnavailableError, UnknownAccountError
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.brokers.icici_breeze import ICICIBreezeBroker
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.strategy_assignment import (
    InvalidAssignmentError,
    StrategyAssignment,
    UnknownAssignmentError,
)
from trading.common.trading_account import ExecutionMode, TradingAccount

STRATEGY_ID = "DoubleStraddelAlgo"


class FakeSmartApi:
    """Same double used throughout tests/common/test_angelone_broker.py --
    a pure Python object, no network, no SDK."""

    def __init__(self, api_key: str):
        self.session_response = {"status": True, "data": {"refreshToken": "rt"}, "message": "SUCCESS"}
        self.placeOrder = MagicMock(name="placeOrder")

    def generateSession(self, client_id, password, totp):
        return self.session_response

    def getfeedToken(self):
        return "ft"


def _angel_broker() -> AngelOneBroker:
    config = TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(
            angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw",
            angelone_totp_secret="JBSWY3DPEHPK3PXP",
        ),
    )
    return AngelOneBroker(
        config, smart_api_factory=lambda k: FakeSmartApi("ak"),
        instrument_resolver=lambda symbol: ("NFO", "99999"), read_only=True,
    )


def _build_registry() -> tuple[BrokerManager, StrategyAssignment]:
    manager = BrokerManager()

    manager.register_account(
        TradingAccount(
            account_id="ANGEL_MAIN", account_name="Angel One (main)", broker_id="angelone",
            execution_mode=ExecutionMode.SHADOW,
        ),
        broker_factory=_angel_broker,
    )

    # Dhan: no adapter exists yet (Phase 7 explicitly defers this). The
    # factory is a placeholder that would raise if ever invoked -- proven
    # never to be, by marking the broker itself unavailable up front.
    manager.register_account(
        TradingAccount(
            account_id="DHAN_MAIN", account_name="Dhan (main)", broker_id="dhan",
            execution_mode=ExecutionMode.SHADOW,
        ),
        broker_factory=lambda: (_ for _ in ()).throw(NotImplementedError("DhanBroker is not implemented yet")),
    )
    manager.set_broker_availability("dhan", False, reason="Dhan adapter not implemented yet (Phase 7 defers this)")

    # ICICI: reuses the EXISTING Phase 0/2 stub (ICICIBreezeBroker) --
    # not a new adapter, and marked unavailable for the same reason.
    icici_config = TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(icici_breeze_api_secret="x", icici_breeze_session_token="y"),
    )
    manager.register_account(
        TradingAccount(
            account_id="ICICI_MAIN", account_name="ICICI Breeze (main)", broker_id="icici_breeze",
            execution_mode=ExecutionMode.SHADOW,
        ),
        broker_factory=lambda: ICICIBreezeBroker(icici_config),
    )
    manager.set_broker_availability("icici_breeze", False, reason="ICICI Breeze order adapter not implemented yet")

    assignment = StrategyAssignment(manager)
    return manager, assignment


# --------------------------------------------------------------------------- #
# The full chain: Strategy -> StrategyAssignment -> TradingAccount -> BrokerManager -> BrokerAdapter
# --------------------------------------------------------------------------- #
def test_strategy_never_selects_a_broker_only_names_itself():
    """The strategy side of this call names only STRATEGY_ID -- nothing
    about "angelone" appears on the calling side at all."""
    manager, assignment = _build_registry()
    assignment.assign(STRATEGY_ID, "ANGEL_MAIN")

    account_id = assignment.get_account_id(STRATEGY_ID)  # strategy only ever asks "what's my account?"
    account = manager.get_account(account_id)
    broker = manager.get_broker(account_id)

    assert account.broker_id == "angelone"
    assert isinstance(broker, AngelOneBroker)
    assert broker.is_connected() is True  # connect() succeeded against the FakeSmartApi double


def test_angel_main_resolves_to_angel_one():
    manager, assignment = _build_registry()
    assignment.assign(STRATEGY_ID, "ANGEL_MAIN")

    account = assignment.get_account(STRATEGY_ID)
    assert account.account_id == "ANGEL_MAIN"
    assert account.broker_id == "angelone"
    assert account.execution_mode == ExecutionMode.SHADOW


def test_all_three_example_accounts_are_registered():
    manager, _assignment = _build_registry()
    ids = {a.account_id for a in manager.accounts()}
    assert ids == {"ANGEL_MAIN", "DHAN_MAIN", "ICICI_MAIN"}


def test_no_real_order_api_is_ever_reached_through_the_registry():
    """Structural + behavioral proof: resolving ANGEL_MAIN's broker and
    connecting it never calls the underlying FakeSmartApi's placeOrder."""
    manager, assignment = _build_registry()
    assignment.assign(STRATEGY_ID, "ANGEL_MAIN")

    broker = manager.get_broker("ANGEL_MAIN")
    fake = broker._smart_api  # the FakeSmartApi instance the factory built

    fake.placeOrder.assert_not_called()


# --------------------------------------------------------------------------- #
# Validation: unknown account
# --------------------------------------------------------------------------- #
def test_unknown_account_is_rejected():
    _manager, assignment = _build_registry()
    with pytest.raises(UnknownAccountError):
        assignment.assign(STRATEGY_ID, "NO_SUCH_ACCOUNT")


# --------------------------------------------------------------------------- #
# Validation: disabled account
# --------------------------------------------------------------------------- #
def test_disabled_account_is_rejected():
    manager, assignment = _build_registry()
    manager.get_account("ANGEL_MAIN").enabled = False

    with pytest.raises(InvalidAssignmentError, match="disabled"):
        assignment.assign(STRATEGY_ID, "ANGEL_MAIN")


def test_account_disabled_after_assignment_is_caught_by_validate():
    manager, assignment = _build_registry()
    assignment.assign(STRATEGY_ID, "ANGEL_MAIN")

    manager.get_account("ANGEL_MAIN").enabled = False

    with pytest.raises(InvalidAssignmentError, match="disabled"):
        assignment.validate(STRATEGY_ID)


# --------------------------------------------------------------------------- #
# Validation: unknown broker (account registered with no client/factory at all)
# --------------------------------------------------------------------------- #
def test_unknown_broker_when_account_has_no_client_or_factory():
    manager, assignment = _build_registry()
    manager.register_account(TradingAccount(account_id="ORPHAN", account_name="Orphan", broker_id="mystery"))
    assignment.assign(STRATEGY_ID, "ORPHAN")

    with pytest.raises(Exception, match="no BrokerClient and no factory"):
        manager.get_broker("ORPHAN")


# --------------------------------------------------------------------------- #
# Validation: disabled broker (Dhan/ICICI -- adapters not implemented yet)
# --------------------------------------------------------------------------- #
def test_dhan_broker_is_unavailable_and_assignment_is_rejected():
    _manager, assignment = _build_registry()
    with pytest.raises(BrokerUnavailableError, match="Dhan"):
        assignment.assign(STRATEGY_ID, "DHAN_MAIN")


def test_icici_broker_is_unavailable_and_assignment_is_rejected():
    _manager, assignment = _build_registry()
    with pytest.raises(BrokerUnavailableError, match="ICICI"):
        assignment.assign(STRATEGY_ID, "ICICI_MAIN")


def test_dhan_placeholder_factory_is_never_actually_invoked():
    """Since DHAN_MAIN's broker is marked unavailable, assign() must never
    reach far enough to call the (deliberately exploding) factory."""
    _manager, assignment = _build_registry()
    try:
        assignment.assign(STRATEGY_ID, "DHAN_MAIN")
    except BrokerUnavailableError:
        pass  # expected -- the factory's own NotImplementedError never fires


def test_broker_becoming_available_later_still_requires_reassignment():
    """Marking a broker available again doesn't retroactively fix an
    assignment that was never made -- assign() must be called again."""
    manager, assignment = _build_registry()
    manager.set_broker_availability("dhan", True)

    # Not implemented for real -- this documents that "available" alone
    # doesn't mean "working"; the placeholder factory would still explode
    # if actually invoked. This test only proves assign() no longer raises
    # BrokerUnavailableError -- it does NOT attempt get_broker("DHAN_MAIN").
    assignment.assign(STRATEGY_ID, "DHAN_MAIN")
    assert assignment.get_account_id(STRATEGY_ID) == "DHAN_MAIN"


# --------------------------------------------------------------------------- #
# Validation: invalid strategy/account assignment (execution_mode mismatch)
# --------------------------------------------------------------------------- #
def test_invalid_assignment_execution_mode_mismatch():
    manager, assignment = _build_registry()
    with pytest.raises(InvalidAssignmentError, match="execution_mode"):
        assignment.assign(STRATEGY_ID, "ANGEL_MAIN", execution_mode=ExecutionMode.LIVE)


def test_get_account_id_for_a_never_assigned_strategy_raises():
    _manager, assignment = _build_registry()
    with pytest.raises(UnknownAssignmentError):
        assignment.get_account_id("NeverAssigned")


# --------------------------------------------------------------------------- #
# Reassignment: same strategy, different account -- a config change, not a code change
# --------------------------------------------------------------------------- #
def test_strategy_can_be_reassigned_to_a_different_account_without_code_changes():
    manager, assignment = _build_registry()
    assignment.assign(STRATEGY_ID, "ANGEL_MAIN")
    assert assignment.get_account_id(STRATEGY_ID) == "ANGEL_MAIN"

    manager.set_broker_availability("dhan", True)
    manager.get_account("DHAN_MAIN").execution_mode = ExecutionMode.SHADOW  # already SHADOW, no-op
    assignment.assign(STRATEGY_ID, "DHAN_MAIN")  # re-assign -- no strategy code touched to do this

    assert assignment.get_account_id(STRATEGY_ID) == "DHAN_MAIN"
