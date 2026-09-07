"""
Straddle Pulse -- per-underlying configuration.

The generic services (ExpiryCycleService, DailySessionService,
ATMSelectionService, OIService) all take ``underlying`` as an explicit
input; this registry is the one place that says what NIFTY vs SENSEX
*mean* (which exchange, which strike-range setting) so no per-underlying
subclassing is needed anywhere else.

Strike step itself is never configured here -- it is always derived from
the listed contracts (``option_chain.strike_step``).
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.core.config import Settings
from trading.market_data.symbols import Exchange


@dataclass(frozen=True)
class UnderlyingConfig:
    symbol: str
    spot_exchange: Exchange
    option_exchange: Exchange
    strike_range_setting_name: str  # attribute name on Settings

    def strike_range(self, settings: Settings) -> int:
        return getattr(settings, self.strike_range_setting_name)


UNDERLYING_CONFIGS: dict[str, UnderlyingConfig] = {
    "NIFTY": UnderlyingConfig("NIFTY", Exchange.NSE, Exchange.NFO, "nifty_option_strike_range"),
    "SENSEX": UnderlyingConfig("SENSEX", Exchange.BSE, Exchange.BFO, "sensex_option_strike_range"),
}

STRADDLE_PULSE_UNDERLYINGS: tuple[str, ...] = tuple(UNDERLYING_CONFIGS.keys())


def underlying_config(symbol: str) -> UnderlyingConfig:
    key = symbol.strip().upper()
    try:
        return UNDERLYING_CONFIGS[key]
    except KeyError:
        raise KeyError(f"Unknown Straddle Pulse underlying: {symbol!r}") from None
