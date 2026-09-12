"""
StrategyAssignment -- strategy_id -> Assignment(account_id, execution_mode,
risk_profile, enabled).

Deliberately NOT strategy_id -> broker: a strategy is bound to an account
(e.g. "ANGEL_MAIN"), and which broker that account happens to use today is
an implementation detail the strategy never sees. Re-pointing an account's
broker_id (or handing a strategy a different account entirely) is a config
change here, not a change to strategy source. Multi-account routing
(Phase 7) means several accounts can exist side by side (ANGEL_MAIN,
DHAN_MAIN, ICICI_MAIN, ...) with strategies routed to whichever one is
assigned -- a strategy never selects a broker directly.

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

from dataclasses import dataclass

from trading.common.broker_manager import BrokerManager
from trading.common.trading_account import ExecutionMode, TradingAccount


class UnknownAssignmentError(KeyError):
    """Raised when a strategy_id has no assignment."""


class InvalidAssignmentError(ValueError):
    """Raised when a strategy is (or would be) assigned to an account that
    doesn't exist, isn't enabled, whose broker is unavailable, or whose
    requested execution_mode doesn't match the account's own."""


@dataclass
class Assignment:
    """The full assignment record -- strategy_id, account_id, and the
    per-assignment execution_mode/risk_profile/enabled fields Phase 7 adds.

    risk_profile is a plain label (e.g. "default", "conservative") --
    Phase 7 only DEFINES it; mapping a label to a concrete RiskLimits
    preset is future work (see docs/phase-7-routing-report.md), not
    implemented here, to avoid re-opening Phase 6's RiskManager scope.
    """

    strategy_id: str
    account_id: str
    execution_mode: ExecutionMode
    risk_profile: str = "default"
    enabled: bool = True


class StrategyAssignment:
    def __init__(self, broker_manager: BrokerManager) -> None:
        self._broker_manager = broker_manager
        self._assignments: dict[str, Assignment] = {}

    def assign(
        self,
        strategy_id: str,
        account_id: str,
        *,
        execution_mode: ExecutionMode | str | None = None,
        risk_profile: str = "default",
        enabled: bool = True,
    ) -> None:
        """Assign strategy_id -> account_id (+ execution_mode/risk_profile/
        enabled). Raises UnknownAccountError if the account isn't
        registered, BrokerUnavailableError if the account's broker_id has
        been marked unavailable (see BrokerManager.set_broker_availability),
        InvalidAssignmentError if the account is disabled or if
        execution_mode is given and doesn't match the account's own.

        execution_mode defaults to None -- meaning "adopt the account's own
        execution_mode as-is" -- rather than defaulting to a hard-coded
        value, so existing 2-argument assign(strategy_id, account_id) calls
        never hit a manufactured mismatch against whatever mode the account
        already happens to be in.
        """
        account = self._broker_manager.get_account(account_id)  # raises UnknownAccountError
        if not account.enabled:
            raise InvalidAssignmentError(
                f"Account '{account_id}' is disabled; cannot assign '{strategy_id}' to it."
            )
        self._broker_manager.require_broker_available(account.broker_id)  # raises BrokerUnavailableError

        resolved_mode = ExecutionMode(execution_mode) if execution_mode is not None else account.execution_mode
        if resolved_mode != account.execution_mode:
            raise InvalidAssignmentError(
                f"Requested execution_mode={resolved_mode.value!r} for '{strategy_id}' does not match "
                f"account '{account_id}''s own execution_mode={account.execution_mode.value!r}."
            )

        self._assignments[strategy_id] = Assignment(
            strategy_id=strategy_id, account_id=account_id, execution_mode=resolved_mode,
            risk_profile=risk_profile, enabled=enabled,
        )

    def get_assignment(self, strategy_id: str) -> Assignment:
        try:
            return self._assignments[strategy_id]
        except KeyError:
            raise UnknownAssignmentError(
                f"No trading-account assignment for strategy '{strategy_id}'."
            ) from None

    def get_account_id(self, strategy_id: str) -> str:
        return self.get_assignment(strategy_id).account_id

    def has_assignment(self, strategy_id: str) -> bool:
        return strategy_id in self._assignments

    def remove(self, strategy_id: str) -> None:
        self._assignments.pop(strategy_id, None)

    def validate(self, strategy_id: str) -> None:
        """Re-check that an existing assignment is still healthy: the
        assignment itself is enabled, the account it points at still
        exists and is enabled, and the account's broker is still
        available. Called before every execution, since any of these can
        change after assign() without going through this class again."""
        assignment = self.get_assignment(strategy_id)
        if not assignment.enabled:
            raise InvalidAssignmentError(f"Assignment for strategy '{strategy_id}' is disabled.")
        account = self._broker_manager.get_account(assignment.account_id)
        if not account.enabled:
            raise InvalidAssignmentError(
                f"Account '{assignment.account_id}' assigned to strategy '{strategy_id}' is disabled."
            )
        self._broker_manager.require_broker_available(account.broker_id)

    def get_account(self, strategy_id: str) -> TradingAccount:
        """Resolve strategy_id -> its assigned TradingAccount object (not
        just the id). Raises the same errors as get_account_id()/
        BrokerManager.get_account() would."""
        return self._broker_manager.get_account(self.get_account_id(strategy_id))
