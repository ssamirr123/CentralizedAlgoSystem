"""
Phase 10 (Section 11/13) -- deterministic strategy templates.

Every supported strategy is a fixed, ordered tuple of ``LegTemplate``
objects. An LLM never invents leg structures (Section 13/63): the
candidate generator (``candidates.py``) resolves each template's
strikes against a real, already-fetched option chain, and that is the
ONLY path by which a ``StrategyCandidate`` can come into existence.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from trading.market_data.options_intelligence import OptionType


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class StrategyType(str, Enum):
    BULL_PUT_CREDIT_SPREAD = "BULL_PUT_CREDIT_SPREAD"
    BEAR_CALL_CREDIT_SPREAD = "BEAR_CALL_CREDIT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"
    IRON_FLY = "IRON_FLY"
    SHORT_STRADDLE = "SHORT_STRADDLE"
    HEDGED_SHORT_STRADDLE = "HEDGED_SHORT_STRADDLE"
    SHORT_STRANGLE = "SHORT_STRANGLE"
    HEDGED_SHORT_STRANGLE = "HEDGED_SHORT_STRANGLE"
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    BULL_CALL_DEBIT_SPREAD = "BULL_CALL_DEBIT_SPREAD"
    BEAR_PUT_DEBIT_SPREAD = "BEAR_PUT_DEBIT_SPREAD"
    LONG_STRADDLE = "LONG_STRADDLE"
    LONG_STRANGLE = "LONG_STRANGLE"
    CALENDAR_SPREAD = "CALENDAR_SPREAD"
    DOUBLE_CALENDAR = "DOUBLE_CALENDAR"


# Section 11: strategies this platform cannot safely model with a single-
# expiry chain snapshot -- a calendar spread's near-leg-expiry payoff
# depends on repricing the far leg (a full pricing-model exercise, not a
# deterministic expiration-payoff formula), so it is marked unsupported
# rather than approximated (Section 11's own explicit escape hatch).
UNSUPPORTED_STRATEGIES: frozenset[StrategyType] = frozenset({
    StrategyType.CALENDAR_SPREAD, StrategyType.DOUBLE_CALENDAR,
})

# Structures whose maximum loss is mathematically undefined/unbounded
# under at least one tail (naked short exposure) -- Section 12: never
# silently treated as defined-risk.
UNDEFINED_RISK_STRATEGIES: frozenset[StrategyType] = frozenset({
    StrategyType.SHORT_STRADDLE, StrategyType.SHORT_STRANGLE,
    StrategyType.LONG_CALL,  # unbounded profit, not risk -- flagged separately as unbounded PROFIT
})


class StrikeSelector(str, Enum):
    ATM = "ATM"                # the chain's ATM strike
    DELTA_TARGET = "DELTA_TARGET"  # nearest strike whose |delta| falls in [min_delta, max_delta]
    WING = "WING"               # offset from a referenced leg's strike by the configured wing width


@dataclass(frozen=True)
class LegTemplate:
    side: Side
    option_type: OptionType
    selector: StrikeSelector
    wing_of: int | None = None  # index of the leg this WING leg is offset from
    use_hedge_width: bool = False  # True -> use the (wider) hedge_wing_width config, not wing_width


# Section 13: fixed leg ordering per strategy. WING direction is always
# derived from option_type (PUT wing = strike - width, CALL wing =
# strike + width) -- see candidates.py -- which correctly produces a
# protective wing for iron condor/fly/hedged straddle/strangle AND the
# short leg of a debit spread (bull call spread's short call sits ABOVE
# its long call; bear put spread's short put sits BELOW its long put).
STRATEGY_TEMPLATES: dict[StrategyType, tuple[LegTemplate, ...]] = {
    StrategyType.BULL_PUT_CREDIT_SPREAD: (
        LegTemplate(Side.SELL, OptionType.PUT, StrikeSelector.DELTA_TARGET),
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.WING, wing_of=0),
    ),
    StrategyType.BEAR_CALL_CREDIT_SPREAD: (
        LegTemplate(Side.SELL, OptionType.CALL, StrikeSelector.DELTA_TARGET),
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.WING, wing_of=0),
    ),
    StrategyType.IRON_CONDOR: (
        LegTemplate(Side.SELL, OptionType.PUT, StrikeSelector.DELTA_TARGET),
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.WING, wing_of=0),
        LegTemplate(Side.SELL, OptionType.CALL, StrikeSelector.DELTA_TARGET),
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.WING, wing_of=2),
    ),
    StrategyType.IRON_FLY: (
        LegTemplate(Side.SELL, OptionType.PUT, StrikeSelector.ATM),
        LegTemplate(Side.SELL, OptionType.CALL, StrikeSelector.ATM),
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.WING, wing_of=0),
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.WING, wing_of=1),
    ),
    StrategyType.SHORT_STRADDLE: (
        LegTemplate(Side.SELL, OptionType.PUT, StrikeSelector.ATM),
        LegTemplate(Side.SELL, OptionType.CALL, StrikeSelector.ATM),
    ),
    StrategyType.HEDGED_SHORT_STRADDLE: (
        LegTemplate(Side.SELL, OptionType.PUT, StrikeSelector.ATM),
        LegTemplate(Side.SELL, OptionType.CALL, StrikeSelector.ATM),
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.WING, wing_of=0, use_hedge_width=True),
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.WING, wing_of=1, use_hedge_width=True),
    ),
    StrategyType.SHORT_STRANGLE: (
        LegTemplate(Side.SELL, OptionType.PUT, StrikeSelector.DELTA_TARGET),
        LegTemplate(Side.SELL, OptionType.CALL, StrikeSelector.DELTA_TARGET),
    ),
    StrategyType.HEDGED_SHORT_STRANGLE: (
        LegTemplate(Side.SELL, OptionType.PUT, StrikeSelector.DELTA_TARGET),
        LegTemplate(Side.SELL, OptionType.CALL, StrikeSelector.DELTA_TARGET),
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.WING, wing_of=0, use_hedge_width=True),
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.WING, wing_of=1, use_hedge_width=True),
    ),
    StrategyType.LONG_CALL: (
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.ATM),
    ),
    StrategyType.LONG_PUT: (
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.ATM),
    ),
    StrategyType.BULL_CALL_DEBIT_SPREAD: (
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.ATM),
        LegTemplate(Side.SELL, OptionType.CALL, StrikeSelector.WING, wing_of=0),
    ),
    StrategyType.BEAR_PUT_DEBIT_SPREAD: (
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.ATM),
        LegTemplate(Side.SELL, OptionType.PUT, StrikeSelector.WING, wing_of=0),
    ),
    StrategyType.LONG_STRADDLE: (
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.ATM),
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.ATM),
    ),
    StrategyType.LONG_STRANGLE: (
        LegTemplate(Side.BUY, OptionType.PUT, StrikeSelector.DELTA_TARGET),
        LegTemplate(Side.BUY, OptionType.CALL, StrikeSelector.DELTA_TARGET),
    ),
}


def is_supported(strategy: StrategyType) -> bool:
    return strategy not in UNSUPPORTED_STRATEGIES
