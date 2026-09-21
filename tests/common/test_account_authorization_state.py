"""Phase 15B section 15: TradingAccount.authorization_state."""
from __future__ import annotations

import pytest

from trading.common.trading_account import AccountAuthorizationState, TradingAccount


def _account(**overrides) -> TradingAccount:
    defaults = dict(account_id="A1", account_name="A1", broker_id="angelone")
    defaults.update(overrides)
    return TradingAccount(**defaults)


def test_default_authorization_state_is_read_only():
    account = _account()
    assert account.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account.is_live_authorized() is False


def test_new_account_defaults_to_read_only_flag_true():
    """A brand-new account must start in the safest posture -- read_only
    defaults to True, matching authorization_state's own safe default."""
    assert _account().read_only is True


def test_is_live_authorized_requires_exact_state_and_enabled():
    account = _account(authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED)
    assert account.is_live_authorized() is True
    account.enabled = False
    assert account.is_live_authorized() is False


@pytest.mark.parametrize(
    "state",
    [
        AccountAuthorizationState.DISABLED,
        AccountAuthorizationState.READ_ONLY,
        AccountAuthorizationState.CANARY_READY,
        AccountAuthorizationState.KILLED,
    ],
)
def test_only_live_authorized_state_passes_is_live_authorized(state):
    account = _account(authorization_state=state)
    assert account.is_live_authorized() is False


def test_set_killed_is_irreversible_and_has_no_undo_method():
    account = _account(authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED)
    assert account.is_live_authorized() is True
    account.set_killed(reason="suspected compromise")
    assert account.authorization_state == AccountAuthorizationState.KILLED
    assert account.is_live_authorized() is False
    assert account.metadata["kill_reason"] == "suspected compromise"
    assert not hasattr(account, "un_kill") and not hasattr(account, "revive") and not hasattr(account, "resume")


def test_authorization_state_accepts_plain_string_like_execution_mode():
    account = _account(authorization_state="CANARY_READY")
    assert account.authorization_state == AccountAuthorizationState.CANARY_READY


def test_owner_and_display_fields_default_to_empty_and_never_break_repr():
    account = _account()
    assert account.owner_id == ""
    assert account.display_name == ""
    assert "A1" in repr(account)
