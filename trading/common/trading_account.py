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
and hold a live BrokerClient for one account. broker_client is excluded
from repr() so printing/logging a TradingAccount can never leak whatever a
real adapter's session object might otherwise expose.
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


@dataclass
class TradingAccount:
    account_id: str
    account_name: str
    broker_id: str
    enabled: bool = True
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    metadata: dict[str, Any] = field(default_factory=dict)
    # Populated by BrokerManager once connected. repr=False so this never
    # gets printed/logged incidentally (see module docstring).
    broker_client: BrokerClient | None = field(default=None, repr=False)

    def is_available(self) -> bool:
        """True only when the account is both administratively enabled AND
        actually holds a live, connected BrokerClient."""
        return self.enabled and self.connection_state == ConnectionState.CONNECTED

    def __repr__(self) -> str:
        return (
            f"TradingAccount(account_id={self.account_id!r}, broker_id={self.broker_id!r}, "
            f"enabled={self.enabled}, connection_state={self.connection_state.value})"
        )
