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

from trading.common.alerts import AlertManager
from trading.common.broker import BrokerClient, BrokerConnectionError, create_broker
from trading.common.config import TradingConfig
from trading.common.observability import (
    EVENT_BROKER_CONNECTED,
    EVENT_BROKER_DISCONNECTED,
    AuditTrail,
    MetricsRegistry,
)
from trading.common.trading_account import ConnectionState, TradingAccount


class UnknownAccountError(KeyError):
    """Raised when an account_id has no registered TradingAccount."""


class BrokerUnavailableError(RuntimeError):
    """Raised when an account's broker_id has been explicitly marked
    unavailable (e.g. its adapter isn't implemented yet -- Dhan/ICICI as of
    Phase 7 -- or it's mid-outage). Distinct from an individual ACCOUNT
    being disabled (TradingAccount.enabled=False): this is a statement
    about the BROKER TYPE, independent of which account uses it."""


class BrokerManager:
    def __init__(
        self,
        *,
        metrics_registry: MetricsRegistry | None = None,
        audit_trail: AuditTrail | None = None,
        alerts: AlertManager | None = None,
    ) -> None:
        self._accounts: dict[str, TradingAccount] = {}
        self._factories: dict[str, Callable[[], BrokerClient]] = {}
        # broker_id -> (available, reason). A broker_id never explicitly
        # registered here defaults to available=True -- existing callers
        # that never call set_broker_availability() see no behavior change.
        self._broker_availability: dict[str, tuple[bool, str]] = {}
        # Phase 13 observability -- all optional, all default None.
        self._metrics_registry = metrics_registry
        self._audit_trail = audit_trail
        self._alerts = alerts

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
            if self._metrics_registry is not None:
                self._metrics_registry.record_account_status(account.account_id, account.connection_state.value)
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
            except Exception as exc:
                account.connection_state = ConnectionState.ERROR
                if self._metrics_registry is not None:
                    self._metrics_registry.record_account_status(account_id, ConnectionState.ERROR.value)
                    self._metrics_registry.record_error(f"broker:{account.broker_id}")
                if self._audit_trail is not None:
                    self._audit_trail.append(
                        EVENT_BROKER_DISCONNECTED, strategy_id="", account_id=account_id,
                        broker_id=account.broker_id, error=str(exc),
                    )
                if self._alerts is not None:
                    self._alerts.broker_disconnected(account.broker_id, reason=str(exc))
                raise
            account.broker_client = client
            account.connection_state = ConnectionState.CONNECTED
            if self._metrics_registry is not None:
                self._metrics_registry.record_broker_heartbeat(account.broker_id)
                self._metrics_registry.record_account_status(account_id, ConnectionState.CONNECTED.value)
            if self._audit_trail is not None:
                self._audit_trail.append(
                    EVENT_BROKER_CONNECTED, account_id=account_id, broker_id=account.broker_id,
                )
        return account.broker_client

    def accounts(self) -> list[TradingAccount]:
        return list(self._accounts.values())

    def broker_ids(self) -> list[str]:
        """All broker_ids known to this manager: every registered account's
        broker_id, plus any broker_id explicitly given an availability
        record via set_broker_availability() even if no account currently
        uses it. Read-only, additive -- lets a caller (e.g. Phase 11's
        control-center API) enumerate brokers without reaching into this
        class's private state."""
        ids = {account.broker_id for account in self._accounts.values()}
        ids.update(self._broker_availability.keys())
        return sorted(ids)

    def broker_status(self, broker_id: str) -> tuple[bool, str]:
        """(available, reason) for a broker_id -- the same pair
        is_broker_available()/require_broker_available() already consult,
        exposed as a read for callers that want both the flag and the
        human-readable reason at once."""
        return self._broker_availability.get(broker_id, (True, ""))

    # -- broker-level (not account-level) availability ---------------------------- #
    def set_broker_availability(self, broker_id: str, available: bool, reason: str = "") -> None:
        """Mark a broker TYPE (e.g. "dhan", "icici_breeze") as available or
        not -- e.g. because its adapter isn't implemented yet, or it's
        mid-outage. Independent of any specific account's own enabled flag."""
        self._broker_availability[broker_id] = (available, reason)

    def is_broker_available(self, broker_id: str) -> bool:
        return self._broker_availability.get(broker_id, (True, ""))[0]

    def require_broker_available(self, broker_id: str) -> None:
        available, reason = self._broker_availability.get(broker_id, (True, ""))
        if not available:
            raise BrokerUnavailableError(f"Broker '{broker_id}' is unavailable: {reason or 'not configured'}")
