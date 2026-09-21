"""
BrokerAdapterFactory + create_broker_for_account (Phase 15B section 9/6).

Additive alongside trading.common.broker.create_broker(), which remains
untouched and is still used by every existing single-global-account caller.
This module adds the per-account path: given a TradingAccount (with its own
broker_id + credential_reference) and a base TradingConfig (for
process-wide, non-credential settings like trading_mode), build a fresh,
per-account TradingConfig whose `credentials` came from THIS account's own
credential_reference, then hand it to the exact same adapter classes
trading.common.broker.create_broker() already uses -- no adapter internals
change at all.

Fails closed: an unregistered/unknown broker_id raises UnsupportedBrokerError
before any adapter is constructed. There is no default adapter.
"""
from __future__ import annotations

import dataclasses
from typing import Callable

from trading.common.broker import BrokerClient
from trading.common.broker_types import BrokerType, UnsupportedBrokerError, broker_type_for_id
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.credentials import AccountCredentials, resolve_credentials


def _to_broker_credentials(creds: AccountCredentials) -> BrokerCredentials:
    """AccountCredentials (this module's per-account shape) -> the existing
    BrokerCredentials dataclass every adapter already reads from
    TradingConfig.credentials. Only the fields relevant to creds.broker_type
    are populated; every other broker's fields are left at their empty
    default, exactly as they would be for a single-account TradingConfig
    that never set them."""
    if creds.broker_type == BrokerType.ANGEL_ONE:
        return BrokerCredentials(
            angelone_api_key=creds.api_key,
            angelone_client_id=creds.client_id,
            angelone_password=creds.password,
            angelone_totp_secret=creds.totp_secret,
        )
    if creds.broker_type == BrokerType.DHAN:
        return BrokerCredentials(dhan_client_id=creds.client_id, dhan_access_token=creds.access_token)
    if creds.broker_type == BrokerType.ICICI_BREEZE:
        return BrokerCredentials(
            icici_breeze_api_key=creds.api_key,
            icici_breeze_api_secret=creds.api_secret,
            icici_breeze_session_token=creds.session_token,
        )
    if creds.broker_type == BrokerType.ZERODHA:
        return BrokerCredentials(
            zerodha_api_key=creds.api_key,
            zerodha_api_secret=creds.api_secret,
            zerodha_access_token=creds.access_token,
        )
    return BrokerCredentials()  # PAPER: no credentials needed


#: BrokerType -> (adapter class import path, whether it accepts read_only=).
#: A lazy-import factory function per broker mirrors trading.common.broker.
#: create_broker()'s own lazy-import style (adapters/SDKs must not be
#: imported unless that specific broker is actually requested).
def _build_angelone(config: TradingConfig, read_only: bool | None) -> BrokerClient:
    from trading.common.brokers.angelone import AngelOneBroker

    return AngelOneBroker(config, read_only=read_only)


def _build_dhan(config: TradingConfig, read_only: bool | None) -> BrokerClient:
    from trading.common.brokers.dhan import DhanBroker

    return DhanBroker(config, read_only=read_only)


def _build_icici_breeze(config: TradingConfig, read_only: bool | None) -> BrokerClient:
    from trading.common.brokers.icici_breeze import ICICIBreezeBroker

    return ICICIBreezeBroker(config, read_only=read_only)


def _build_zerodha(config: TradingConfig, read_only: bool | None) -> BrokerClient:
    from trading.common.brokers.zerodha_kite import ZerodhaKiteBroker

    return ZerodhaKiteBroker(config)  # no read_only support today -- see module TODOs


def _build_paper(config: TradingConfig, read_only: bool | None) -> BrokerClient:
    from trading.common.brokers.paper_broker import PaperBroker

    return PaperBroker()


_BUILDERS: dict[BrokerType, Callable[[TradingConfig, bool | None], BrokerClient]] = {
    BrokerType.ANGEL_ONE: _build_angelone,
    BrokerType.DHAN: _build_dhan,
    BrokerType.ICICI_BREEZE: _build_icici_breeze,
    BrokerType.ZERODHA: _build_zerodha,
    BrokerType.PAPER: _build_paper,
}


class BrokerAdapterFactory:
    """BrokerType -> adapter class resolution, fail-closed for anything not
    explicitly registered in _BUILDERS above."""

    @staticmethod
    def is_supported(broker_type: BrokerType) -> bool:
        return broker_type in _BUILDERS

    @staticmethod
    def build(config: TradingConfig, broker_type: BrokerType, *, read_only: bool | None = None) -> BrokerClient:
        try:
            builder = _BUILDERS[broker_type]
        except KeyError:
            raise UnsupportedBrokerError(
                f"BrokerAdapterFactory has no adapter registered for {broker_type!r}."
            ) from None
        return builder(config, read_only)


def build_account_config(account, base_config: TradingConfig) -> TradingConfig:
    """TradingAccount -> a fresh TradingConfig carrying THIS account's own
    resolved credentials (via its credential_reference), with every other
    field copied from base_config (trading_mode, broker_reconnect_*, ...).
    Deliberately does not mutate base_config -- TradingConfig is frozen."""
    broker_type = broker_type_for_id(account.broker_id)
    account_creds = resolve_credentials(broker_type, account.credential_reference)
    return dataclasses.replace(
        base_config,
        broker_name=account.broker_id,
        credentials=_to_broker_credentials(account_creds),
    )


def create_broker_for_account(account, base_config: TradingConfig) -> BrokerClient:
    """The Phase 15B per-account replacement for
    trading.common.broker.create_broker(config): resolves account.broker_id
    -> BrokerType -> adapter, using ONLY this account's own credentials
    (never base_config.credentials, which is ignored for the credential
    fields and only supplies process-wide settings like trading_mode).

    account.read_only (if the TradingAccount has that attribute -- see
    trading.common.trading_account's Phase 15B additions) is passed through
    to adapters that support it; accounts without that attribute get
    read_only=None (adapter's own environment-based default)."""
    broker_type = broker_type_for_id(account.broker_id)
    per_account_config = build_account_config(account, base_config)
    read_only = getattr(account, "read_only", None)
    return BrokerAdapterFactory.build(per_account_config, broker_type, read_only=read_only)
