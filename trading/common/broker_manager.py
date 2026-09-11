"""
BrokerManager -- the only place strategy-adjacent code resolves:

    account_id
        |
        v
    TradingAccount
        |
        v
    broker_id
        |
        v
    BrokerClient

It does not itself know how to log in to a broker. An account is
registered either with an already-connected BrokerClient (tests wiring up
a PaperBroker) or with a zero-arg factory that builds one lazily on first
use -- register_account_with_config() is a thin convenience over the
existing trading.common.broker.create_broker() factory so production
accounts don't need a second registry or a hand-rolled construction path.

Phase 1 deliberately has no background reconnect loop and no threads: a
disconnected account is simply reconnected synchronously the next time its
broker is requested. TradingConfig already carries
broker_reconnect_max_attempts/backoff_seconds for whenever a real
reconnect-with-backoff loop is built on top of this.

Never hands a strategy a raw broker SDK object -- only BrokerClient (the
existing broker-agnostic interface) is ever returned.
"""
from __future__ import annotations

from typing import Callable

from trading.common.broker import BrokerClient, BrokerConnectionError, create_broker
from trading.common.config import TradingConfig
from trading.common.trading_account import ConnectionState, TradingAccount


class UnknownAccountError(KeyError):
    """Raised when an account_id has no registered TradingAccount."""


class BrokerManager:
    def __init__(self) -> None:
        self._accounts: dict[str, TradingAccount] = {}
        self._factories: dict[str, Callable[[], BrokerClient]] = {}

    def register_account(
        self,
        account: TradingAccount,
        broker_client: BrokerClient | None = None,
        broker_factory: Callable[[], BrokerClient] | None = None,
    ) -> None:
        """Register a TradingAccount, optionally with an already-built
        BrokerClient (tests, or an account connected elsewhere) or a
        factory to build+connect one lazily on first use (production)."""
        self._accounts[account.account_id] = account
        if broker_client is not None:
            account.broker_client = broker_client
            account.connection_state = (
                ConnectionState.CONNECTED if broker_client.is_connected() else ConnectionState.DISCONNECTED
            )
        elif broker_factory is not None:
            self._factories[account.account_id] = broker_factory

    def register_account_with_config(self, account: TradingAccount, config: TradingConfig) -> None:
        """Convenience: register an account whose BrokerClient is built
        lazily via the existing create_broker() factory, so callers never
        have to hand-write that lambda themselves."""
        self.register_account(account, broker_factory=lambda: create_broker(config))

    def get_account(self, account_id: str) -> TradingAccount:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise UnknownAccountError(account_id) from None

    def is_available(self, account_id: str) -> bool:
        account = self._accounts.get(account_id)
        return account is not None and account.is_available()

    def get_broker(self, account_id: str) -> BrokerClient:
        """Resolve account_id -> BrokerClient, connecting lazily via the
        registered factory if this account hasn't been connected yet."""
        account = self.get_account(account_id)
        if account.broker_client is None:
            factory = self._factories.get(account_id)
            if factory is None:
                raise BrokerConnectionError(
                    f"Account '{account_id}' has no BrokerClient and no factory to create one."
                )
            account.connection_state = ConnectionState.CONNECTING
            try:
                client = factory()
                client.connect()
            except Exception:
                account.connection_state = ConnectionState.ERROR
                raise
            account.broker_client = client
            account.connection_state = ConnectionState.CONNECTED
        return account.broker_client

    def accounts(self) -> list[TradingAccount]:
        return list(self._accounts.values())
