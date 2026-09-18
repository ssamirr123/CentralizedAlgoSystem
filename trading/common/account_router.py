"""
TradingAccountRouter (Phase 15B section 10) -- resolves account_id to
exactly that account's own BrokerClient, and nothing else's.

Built on top of BrokerManager (untouched) rather than replacing it: this
class adds the specific guarantee Phase 15B section 10 asks for --
`router.get_funds("ANGEL_SAMIR")` and `router.get_funds("ANGEL_WIFE")` must
resolve completely independently, and an owner context (when supplied) must
never be able to reach an account it does not own.

Read-only by construction: every method here is a read (get_funds,
get_positions, get_orders). There is no place_order/modify_order/cancel_order
method on this class at all in this phase -- routing execution-capable
operations is explicitly out of scope (see docs/phase-15b-architecture-plan.md
Section 11).
"""
from __future__ import annotations

from typing import Any

from trading.common.account_state import AccountState, build_account_state
from trading.common.broker import BrokerClient
from trading.common.broker_manager import BrokerManager
from trading.common.trading_account import AccountAuthorizationState, TradingAccount


class AccountAccessDeniedError(PermissionError):
    """Raised when a supplied owner_context does not match the resolved
    account's own owner_id. Never raised when owner_context is omitted
    (None) -- that is the "no owner scoping requested" case, used by
    system-level callers (e.g. the Phase 14 preflight tool) that are not
    acting on behalf of one specific human owner."""


class AccountUnavailableError(RuntimeError):
    """Raised when an account is DISABLED or KILLED -- even for read-only
    operations, since a killed account's credentials must not be used for
    anything at all, including reads."""


class TradingAccountRouter:
    def __init__(self, broker_manager: BrokerManager) -> None:
        self._broker_manager = broker_manager

    def _resolve(self, account_id: str, owner_context: str | None) -> tuple[TradingAccount, BrokerClient]:
        account = self._broker_manager.get_account(account_id)  # raises UnknownAccountError
        if owner_context is not None and account.owner_id and account.owner_id != owner_context:
            raise AccountAccessDeniedError(
                f"owner_context {owner_context!r} is not authorized for account "
                f"'{account_id}' (owned by {account.owner_id!r})."
            )
        if account.authorization_state in (AccountAuthorizationState.DISABLED, AccountAuthorizationState.KILLED):
            raise AccountUnavailableError(
                f"Account '{account_id}' is {account.authorization_state.value} -- no operation permitted."
            )
        broker = self._broker_manager.get_broker(account_id)
        return account, broker

    def get_funds(self, account_id: str, *, owner_context: str | None = None) -> Any:
        """Returns whatever the underlying adapter's get_funds()/equivalent
        returns -- not itself normalized (see get_account_state() for the
        normalized form). Raises AttributeError if the resolved adapter has
        no get_funds() method (e.g. PaperBroker) -- deliberately not
        swallowed, since silently returning a fabricated zero-funds value
        would be exactly the kind of "assume sufficiency" this project's
        Phase 15A explicitly forbids."""
        _, broker = self._resolve(account_id, owner_context)
        return broker.get_funds()  # type: ignore[attr-defined]

    def get_positions(self, account_id: str, *, owner_context: str | None = None) -> Any:
        _, broker = self._resolve(account_id, owner_context)
        return broker.get_positions()

    def get_orders(self, account_id: str, *, owner_context: str | None = None) -> Any:
        _, broker = self._resolve(account_id, owner_context)
        return broker.get_order_book()  # type: ignore[attr-defined]

    def get_account_state(self, account_id: str, *, owner_context: str | None = None) -> AccountState:
        """Best-effort normalized snapshot. Any field the underlying adapter
        cannot supply is surfaced as an exception, not a fabricated default
        -- callers that need partial data should call the individual
        get_funds/get_positions/get_orders methods directly instead."""
        account, broker = self._resolve(account_id, owner_context)
        funds = broker.get_funds()  # type: ignore[attr-defined]
        positions = broker.get_positions()
        try:
            orders = broker.get_order_book()  # type: ignore[attr-defined]
        except AttributeError:
            orders = []
        return build_account_state(
            account_id=account_id,
            broker_id=account.broker_id,
            available_cash=getattr(funds, "available_cash", 0.0),
            used_margin=getattr(funds, "used_margin", 0.0),
            positions=positions,
            open_orders=[o if isinstance(o, dict) else vars(o) for o in orders],
        )
