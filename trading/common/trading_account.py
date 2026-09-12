"""
TradingAccount -- a specific brokerage account, not merely "a broker".

Two accounts can share the same broker_id (e.g. ANGEL_MAIN and ANGEL_BACKUP
both broker_id="angelone") yet be entirely separate logins/capital/sessions.
Strategies are assigned to an account_id (see strategy_assignment.py),
never to a broker directly -- that's what makes "Algo1 -> Dhan, Algo2 ->
Shoonya" (and later, swapped) a config change instead of a code change.

Security: this object never stores or exposes secrets (passwords, MPIN,
TOTP seed, API secret, access/refresh tokens). Credential resolution stays
wherever it already lives today (env vars / TradingConfig.credentials);
this class only carries the non-secret identity/config needed to look up
and hold a live BrokerClient for one account. `credential_reference` below
is a NON-secret pointer to where credentials live (e.g. "env:ANGELONE_*"
or a secrets-manager ARN/name) -- it must never itself be, or contain, a
secret value. broker_client is excluded from repr() so printing/logging a
TradingAccount can never leak whatever a real adapter's session object
might otherwise expose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from trading.common.broker import BrokerClient


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class ExecutionMode(str, Enum):
    """Per-account execution mode -- deliberately independent of the
    process-wide TRADING_MODE env var, since a single process can hold
    more than one TradingAccount with different modes (e.g. one PAPER
    account and one SHADOW account at once).

    LIVE_CANARY (Phase 14) is a DISTINCT mode from LIVE -- real orders
    reach a real broker in both, but LIVE_CANARY additionally requires
    every intent to pass trading.common.live_canary.LiveCanaryGuard's
    authorization (dedicated account, tiny quantity/value caps, daily
    order cap, mandatory idempotency, kill switch, ...) between
    RiskManager and StrategyExecutionEngine. Introducing it as its own
    enum member -- rather than overloading LIVE with a "canary" flag
    elsewhere -- means every existing execution_mode == LIVE check
    (ConnectedShadowBroker's fail-closed construction guard, the
    frontend's LIVE_EXECUTION_ENABLED gate, etc.) does NOT accidentally
    treat a canary account as unrestricted LIVE, and vice versa."""

    LIVE = "LIVE"
    LIVE_CANARY = "LIVE_CANARY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"


@dataclass
class TradingAccount:
    account_id: str
    account_name: str
    broker_id: str
    enabled: bool = True
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    # Free-form deployment/environment label (e.g. "production", "staging",
    # "dev") -- informational, not a safety gate. The actual safety gates
    # remain TradingConfig.is_live and, for AngelOneBroker specifically,
    # its own read_only flag -- this field never substitutes for either.
    environment: str = "production"
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    # Non-secret pointer to where this account's credentials are resolved
    # from (e.g. "env:ANGELONE_*", "secretsmanager:angel-main") -- see the
    # module docstring's Security note. Never a credential value itself.
    credential_reference: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # Populated by BrokerManager once connected. repr=False so this never
    # gets printed/logged incidentally (see module docstring).
    broker_client: BrokerClient | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # TradingAccount is a plain (non-frozen) dataclass, so a normal
        # attribute assignment is enough to coerce a plain string into the
        # enum -- unlike OrderIntent's frozen __post_init__.
        self.execution_mode = ExecutionMode(self.execution_mode)

    def is_available(self) -> bool:
        """True only when the account is both administratively enabled AND
        actually holds a live, connected BrokerClient."""
        return self.enabled and self.connection_state == ConnectionState.CONNECTED

    def __repr__(self) -> str:
        return (
            f"TradingAccount(account_id={self.account_id!r}, broker_id={self.broker_id!r}, "
            f"enabled={self.enabled}, connection_state={self.connection_state.value}, "
            f"execution_mode={self.execution_mode.value})"
        )
