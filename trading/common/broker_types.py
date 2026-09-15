"""
BrokerType -- a formal enum identity for every broker, plus a declarative
BrokerCapabilities registry (Phase 15B, section 4/19).

Deliberately separate from TradingAccount.broker_id (a plain string): every
existing account/config/test that already uses broker_id="angelone" etc.
keeps working unchanged. BrokerType is the new, additive, strongly-typed
identity used by the new BrokerAdapterFactory and capability lookups; the
two stay in sync via a fixed BROKER_ID_TO_TYPE mapping below.

Capabilities are configuration, not code: a broker-specific compliance
requirement (e.g. Zerodha's static-IP whitelisting) is declared once here
and read by whoever needs it, rather than being hard-coded into the core
execution path or assumed to apply to every broker.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BrokerType(str, Enum):
    ANGEL_ONE = "ANGEL_ONE"
    DHAN = "DHAN"
    ICICI_BREEZE = "ICICI_BREEZE"
    ZERODHA = "ZERODHA"
    PAPER = "PAPER"


class UnsupportedBrokerError(ValueError):
    """Raised for any broker identity this system does not recognize.
    Fail-closed by design (Phase 15B section 9): there is no default/
    fallback broker -- an unknown value is always an error, never silently
    routed to Angel One or any other broker."""


#: broker_id (the existing free-form string field on TradingAccount /
#: TradingConfig.broker_name) -> BrokerType. Kept as an explicit, closed
#: mapping (not a case-insensitive guess) so a typo in broker_id fails
#: closed instead of silently resolving to the wrong broker.
BROKER_ID_TO_TYPE: dict[str, BrokerType] = {
    "angelone": BrokerType.ANGEL_ONE,
    "dhan": BrokerType.DHAN,
    "icici_breeze": BrokerType.ICICI_BREEZE,
    "zerodha": BrokerType.ZERODHA,
    "paper": BrokerType.PAPER,
}


def broker_type_for_id(broker_id: str) -> BrokerType:
    """Resolve an existing broker_id string to its BrokerType. Fails closed
    (UnsupportedBrokerError) for anything not in BROKER_ID_TO_TYPE -- never
    guesses, never defaults."""
    try:
        return BROKER_ID_TO_TYPE[broker_id]
    except KeyError:
        raise UnsupportedBrokerError(
            f"Unknown broker_id {broker_id!r}. Expected one of: {sorted(BROKER_ID_TO_TYPE)}."
        ) from None


@dataclass(frozen=True)
class BrokerCapabilities:
    """Declarative, broker-specific capabilities. None of these fields are
    safety gates by themselves -- TRADING_MODE, an adapter's own read_only
    flag, LiveCanaryGuard and CentralKillSwitch remain the actual
    enforcement points. This registry exists so the platform never has to
    hard-code "if broker == X" logic outside the adapter layer to know
    whether an operation is even meaningful for that broker."""

    broker_type: BrokerType
    requires_static_ip: bool = False
    supports_websocket: bool = False
    supports_orders: bool = True
    supports_positions: bool = True
    supports_funds: bool = True
    supports_options: bool = True
    supports_market_data: bool = True
    # True only once an adapter's live order-placement path has been
    # explicitly validated against a real account for this broker (see each
    # adapter module's own docstring for its current verification status).
    # This is a documentation-level declaration, not itself a safety gate --
    # every real adapter still independently enforces its own read_only/
    # TRADING_MODE checks regardless of this flag's value.
    supports_live_orders: bool = False


#: One entry per BrokerType -- deliberately explicit rather than a default,
#: so adding a new BrokerType member without registering its capabilities
#: here is caught by get_capabilities() raising KeyError-turned-
#: UnsupportedBrokerError rather than silently inheriting some other
#: broker's capability profile.
_CAPABILITIES: dict[BrokerType, BrokerCapabilities] = {
    BrokerType.ANGEL_ONE: BrokerCapabilities(
        broker_type=BrokerType.ANGEL_ONE,
        requires_static_ip=False,
        supports_websocket=True,
        supports_live_orders=False,  # see trading/common/brokers/angelone.py docstring
    ),
    BrokerType.DHAN: BrokerCapabilities(
        broker_type=BrokerType.DHAN,
        requires_static_ip=False,
        supports_websocket=True,
        supports_live_orders=False,  # Phase 8: unverified against a real account
    ),
    BrokerType.ICICI_BREEZE: BrokerCapabilities(
        broker_type=BrokerType.ICICI_BREEZE,
        requires_static_ip=True,  # ICICI Breeze requires a whitelisted static IP for API access
        supports_websocket=True,
        supports_live_orders=False,  # Phase 9: unverified against a real account
    ),
    BrokerType.ZERODHA: BrokerCapabilities(
        broker_type=BrokerType.ZERODHA,
        requires_static_ip=False,
        supports_websocket=True,
        supports_live_orders=False,
    ),
    BrokerType.PAPER: BrokerCapabilities(
        broker_type=BrokerType.PAPER,
        requires_static_ip=False,
        supports_websocket=False,
        supports_options=True,
        supports_live_orders=False,  # PaperBroker never reaches a real broker at all
    ),
}


def get_capabilities(broker_type: BrokerType) -> BrokerCapabilities:
    try:
        return _CAPABILITIES[broker_type]
    except KeyError:
        raise UnsupportedBrokerError(f"No BrokerCapabilities registered for {broker_type!r}.") from None
