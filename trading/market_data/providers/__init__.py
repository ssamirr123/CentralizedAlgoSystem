"""Market-data provider abstraction + registry.

Add a new provider by implementing ``MarketDataProvider`` and registering
it in ``create_market_data_provider`` -- nothing above this layer changes
(rule 19).
"""
from __future__ import annotations

from typing import Any

from trading.market_data.providers.base import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderDataError,
    ProviderError,
    ProviderRateLimitError,
    TickCallback,
)
from trading.market_data.providers.icici_breeze import ICICIBreezeProvider

__all__ = [
    "MarketDataProvider",
    "ProviderError",
    "ProviderAuthError",
    "ProviderConnectionError",
    "ProviderRateLimitError",
    "ProviderDataError",
    "TickCallback",
    "ICICIBreezeProvider",
    "create_market_data_provider",
]

_PROVIDERS = {
    "icici_breeze": ICICIBreezeProvider,
    "breeze": ICICIBreezeProvider,
}


def create_market_data_provider(name: str, **kwargs: Any) -> MarketDataProvider:
    """Factory mirroring ``trading.common.broker.create_broker``.

    ``kwargs`` are passed straight to the provider constructor (e.g.
    ``api_key`` / ``api_secret`` / ``session_token`` for Breeze). Wiring
    these from ``load_settings()`` happens in the Phase 3 auth/config work.
    """
    key = (name or "").strip().lower()
    try:
        cls = _PROVIDERS[key]
    except KeyError:
        raise ValueError(
            f"Unknown market-data provider {name!r}. Known: {', '.join(sorted(set(_PROVIDERS)))}"
        ) from None
    return cls(**kwargs)
