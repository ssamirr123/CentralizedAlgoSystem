"""Phase 15B: BrokerType enum + BrokerCapabilities registry, fail-closed."""
from __future__ import annotations

import pytest

from trading.common.broker_types import (
    BrokerType,
    UnsupportedBrokerError,
    broker_type_for_id,
    get_capabilities,
)


@pytest.mark.parametrize(
    "broker_id,expected",
    [
        ("angelone", BrokerType.ANGEL_ONE),
        ("dhan", BrokerType.DHAN),
        ("icici_breeze", BrokerType.ICICI_BREEZE),
        ("zerodha", BrokerType.ZERODHA),
        ("paper", BrokerType.PAPER),
    ],
)
def test_broker_type_for_id_resolves_every_known_broker(broker_id, expected):
    assert broker_type_for_id(broker_id) == expected


def test_broker_type_for_id_fails_closed_on_unknown_broker():
    with pytest.raises(UnsupportedBrokerError):
        broker_type_for_id("some_future_broker")


def test_broker_type_for_id_never_defaults_to_angel_one():
    """An unknown broker must never silently resolve to Angel One (or
    anything else) -- it must raise, per Phase 15B section 9's explicit
    'never default to Angel One' rule."""
    for bogus in ("", "ANGELONE", "Angel_One", "kotak", "upstox", "fyers"):
        with pytest.raises(UnsupportedBrokerError):
            broker_type_for_id(bogus)


def test_every_broker_type_has_registered_capabilities():
    for broker_type in BrokerType:
        caps = get_capabilities(broker_type)
        assert caps.broker_type == broker_type


def test_no_broker_declares_live_orders_supported_yet():
    """Every real adapter in this codebase is documented as unverified
    against a live account for order placement (Angel/Dhan/ICICI) or
    structurally incapable of live orders (Paper) -- capabilities must not
    overclaim readiness."""
    for broker_type in BrokerType:
        assert get_capabilities(broker_type).supports_live_orders is False


def test_icici_breeze_requires_static_ip_but_others_do_not_assume_it():
    assert get_capabilities(BrokerType.ICICI_BREEZE).requires_static_ip is True
    assert get_capabilities(BrokerType.ANGEL_ONE).requires_static_ip is False
    assert get_capabilities(BrokerType.DHAN).requires_static_ip is False
