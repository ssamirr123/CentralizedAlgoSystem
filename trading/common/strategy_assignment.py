"""
StrategyAssignment -- strategy_id -> trading_account_id.

Deliberately NOT strategy_id -> broker: a strategy is bound to an account
(e.g. "ANGEL_MAIN"), and which broker that account happens to use today is
an implementation detail the strategy never sees. Re-pointing an account's
broker_id (or handing a strategy a different account entirely) is a config
change here, not a change to strategy source.

Phase 1 keeps this in-memory/config-driven, because nothing today needs it
durable across restarts -- a strategy's assignment is read once at startup
the same way BROKER=... is read today. The public API is shaped so a future
database-backed implementation is a drop-in replacement: nothing above this
class (RiskManager, ExecutionEngine) should ever touch a dict directly,
only call these methods.

Hot reassignment safety (never move a strategy while it has open
orders/positions) is explicitly OUT of scope here -- it belongs to the
eventual production assignment service, once there's something to check
assignments against.
"""
from __future__ import annotations

from trading.common.broker_manager import BrokerManager


class UnknownAssignmentError(KeyError):
    """Raised when a strategy_id has no assignment."""


class InvalidAssignmentError(ValueError):
    """Raised when a strategy is (or would be) assigned to an account that
    doesn't exist or isn't enabled."""


class StrategyAssignment:
    def __init__(self, broker_manager: BrokerManager) -> None:
        self._broker_manager = broker_manager
        self._assignments: dict[str, str] = {}

    def assign(self, strategy_id: str, account_id: str) -> None:
        """Assign strategy_id -> account_id. Raises UnknownAccountError if
        the account isn't registered, InvalidAssignmentError if it exists
        but is disabled."""
        account = self._broker_manager.get_account(account_id)
        if not account.enabled:
            raise InvalidAssignmentError(
                f"Account '{account_id}' is disabled; cannot assign '{strategy_id}' to it."
            )
        self._assignments[strategy_id] = account_id

    def get_account_id(self, strategy_id: str) -> str:
        try:
            return self._assignments[strategy_id]
        except KeyError:
            raise UnknownAssignmentError(
                f"No trading-account assignment for strategy '{strategy_id}'."
            ) from None

    def has_assignment(self, strategy_id: str) -> bool:
        return strategy_id in self._assignments

    def remove(self, strategy_id: str) -> None:
        self._assignments.pop(strategy_id, None)

    def validate(self, strategy_id: str) -> None:
        """Re-check that an existing assignment still points at an
        existent, enabled account -- called before every execution, since
        an account can be disabled after assign() without going through
        this class again."""
        account_id = self.get_account_id(strategy_id)
        account = self._broker_manager.get_account(account_id)
        if not account.enabled:
            raise InvalidAssignmentError(
                f"Account '{account_id}' assigned to strategy '{strategy_id}' is disabled."
            )
