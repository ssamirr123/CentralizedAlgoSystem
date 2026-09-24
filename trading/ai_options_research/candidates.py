"""
Phase 10 (Section 14/15/16) -- deterministic candidate generation.

Strikes are ALWAYS resolved from the real, already-fetched Phase 9
``MarketStructureSummary.rows`` -- never invented. An LLM never sees this
module's internals; it only receives the finished ``StrategyCandidate``
objects to interpret/compare (Section 63).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from trading.ai_options_research.payoff import PayoffResult, ResolvedLeg, compute_payoff, price_leg
from trading.ai_options_research.strategy_templates import (
    STRATEGY_TEMPLATES,
    LegTemplate,
    Side,
    StrategyType,
    StrikeSelector,
    is_supported,
)
from trading.market_data.options_intelligence import MarketStructureSummary, OptionAnalyticsRow, OptionType


@dataclass(frozen=True)
class CandidateGenerationConfig:
    """Section 15: configurable, with safe/conservative defaults.

    min_delta/max_delta: the |delta| band a DELTA_TARGET short/long leg
    must fall in (default ~0.15-0.30, a conventional "30-delta-ish"
    credit-spread/strangle short-strike band).
    wing_width / hedge_wing_width: point-distance (in strikes' own
    units, e.g. NIFTY index points) used to place a protective wing
    relative to its reference leg; hedge_wing_width is used for the
    explicitly "hedged"/wider-protection strategy variants.
    """

    min_delta: float = 0.15
    max_delta: float = 0.30
    minimum_volume: int = 0
    minimum_oi: int = 0
    maximum_bid_ask_spread: float | None = None  # None = not enforced
    wing_width: float = 100.0
    hedge_wing_width: float = 300.0
    strike_window: int = 10  # how far from ATM to search for a DELTA_TARGET match, in listed-strike count


@dataclass(frozen=True)
class CandidateLeg:
    side: Side
    option_type: OptionType
    strike: float
    price: float | None
    price_source: str
    delta: float | None
    iv: float | None
    volume: int | None
    open_interest: int | None
    bid: float | None
    ask: float | None
    liquidity_ok: bool
    liquidity_reasons: tuple[str, ...]


@dataclass(frozen=True)
class StrategyCandidate:
    strategy: StrategyType
    underlying: str
    expiry: object  # date
    supported: bool
    legs: tuple[CandidateLeg, ...]
    payoff: PayoffResult | None
    liquidity_ok: bool
    generation_notes: tuple[str, ...]
    unsupported_reason: str | None = None


def _rows_by_strike(summary: MarketStructureSummary) -> dict[tuple[float, OptionType], OptionAnalyticsRow]:
    return {(r.strike, r.option_type): r for r in summary.rows}


def _check_liquidity(row: OptionAnalyticsRow | None, config: CandidateGenerationConfig) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if row is None or row.quote is None:
        return False, ("no quote available for this strike",)
    q = row.quote
    if config.minimum_volume and (q.volume is None or q.volume < config.minimum_volume):
        reasons.append(f"volume {q.volume} below minimum {config.minimum_volume}")
    if config.minimum_oi and (q.oi is None or q.oi < config.minimum_oi):
        reasons.append(f"open interest {q.oi} below minimum {config.minimum_oi}")
    if config.maximum_bid_ask_spread is not None:
        if q.bid is None or q.ask is None:
            reasons.append("bid/ask unavailable to evaluate spread")
        elif (q.ask - q.bid) > config.maximum_bid_ask_spread:
            reasons.append(f"bid/ask spread {q.ask - q.bid:.2f} exceeds maximum {config.maximum_bid_ask_spread}")
    return (len(reasons) == 0), tuple(reasons)


def _resolve_delta_target(
    option_type: OptionType, summary: MarketStructureSummary, config: CandidateGenerationConfig,
) -> tuple[float | None, str | None]:
    candidates = [
        r for r in summary.rows
        if r.option_type == option_type and r.quote is not None and r.greeks is not None and r.greeks.delta is not None
    ]
    if not candidates:
        return None, "no delta data available on this chain (IV/Greeks could not be computed for any strike)"
    in_band = [r for r in candidates if config.min_delta <= abs(r.greeks.delta) <= config.max_delta]
    target_mid = (config.min_delta + config.max_delta) / 2.0
    if in_band:
        best = min(in_band, key=lambda r: abs(abs(r.greeks.delta) - target_mid))
        return best.strike, None
    # Fallback: nearest available delta to the band, with a transparent note.
    best = min(candidates, key=lambda r: min(abs(abs(r.greeks.delta) - config.min_delta), abs(abs(r.greeks.delta) - config.max_delta)))
    return best.strike, (
        f"no strike found with |delta| in [{config.min_delta}, {config.max_delta}]; "
        f"used nearest available strike {best.strike} (|delta|={abs(best.greeks.delta):.3f})"
    )


def _nearest_real_strike(target: float, strikes: list[float]) -> float:
    return min(strikes, key=lambda s: (abs(s - target), s))


def generate_candidate(
    strategy: StrategyType, summary: MarketStructureSummary, config: CandidateGenerationConfig,
) -> StrategyCandidate:
    if not is_supported(strategy):
        return StrategyCandidate(
            strategy=strategy, underlying=summary.underlying, expiry=summary.expiry, supported=False,
            legs=(), payoff=None, liquidity_ok=False, generation_notes=(),
            unsupported_reason="this platform does not model multi-expiry structures from a single-expiry snapshot",
        )

    templates = STRATEGY_TEMPLATES.get(strategy)
    if templates is None:
        return StrategyCandidate(
            strategy=strategy, underlying=summary.underlying, expiry=summary.expiry, supported=False,
            legs=(), payoff=None, liquidity_ok=False, generation_notes=(),
            unsupported_reason="no template defined for this strategy",
        )

    all_strikes = sorted({r.strike for r in summary.rows})
    if not all_strikes or summary.atm_strike is None:
        return StrategyCandidate(
            strategy=strategy, underlying=summary.underlying, expiry=summary.expiry, supported=False,
            legs=(), payoff=None, liquidity_ok=False, generation_notes=(),
            unsupported_reason="no chain data available to generate candidate strikes",
        )

    by_strike = _rows_by_strike(summary)
    notes: list[str] = []
    resolved_strikes: list[float] = []

    for i, leg in enumerate(templates):
        if leg.selector == StrikeSelector.ATM:
            resolved_strikes.append(summary.atm_strike)
        elif leg.selector == StrikeSelector.DELTA_TARGET:
            strike, note = _resolve_delta_target(leg.option_type, summary, config)
            if strike is None:
                return StrategyCandidate(
                    strategy=strategy, underlying=summary.underlying, expiry=summary.expiry, supported=False,
                    legs=(), payoff=None, liquidity_ok=False, generation_notes=tuple(notes),
                    unsupported_reason=note or "could not resolve a delta-target strike",
                )
            if note:
                notes.append(note)
            resolved_strikes.append(strike)
        elif leg.selector == StrikeSelector.WING:
            ref_strike = resolved_strikes[leg.wing_of]
            width = config.hedge_wing_width if leg.use_hedge_width else config.wing_width
            target = ref_strike - width if leg.option_type == OptionType.PUT else ref_strike + width
            resolved_strikes.append(_nearest_real_strike(target, all_strikes))

    legs: list[CandidateLeg] = []
    payoff_legs: list[ResolvedLeg] = []
    for leg, strike in zip(templates, resolved_strikes):
        row = by_strike.get((strike, leg.option_type))
        ok, reasons = _check_liquidity(row, config)
        q = row.quote if row is not None else None
        price, source = price_leg(
            leg.side, bid=q.bid if q else None, ask=q.ask if q else None, ltp=q.ltp if q else None,
        )
        legs.append(CandidateLeg(
            side=leg.side, option_type=leg.option_type, strike=strike, price=price, price_source=source,
            delta=row.greeks.delta if (row and row.greeks) else None,
            iv=row.iv if row else None,
            volume=q.volume if q else None, open_interest=q.oi if q else None,
            bid=q.bid if q else None, ask=q.ask if q else None,
            liquidity_ok=ok, liquidity_reasons=reasons,
        ))
        payoff_legs.append(ResolvedLeg(
            side=leg.side, option_type=leg.option_type, strike=strike, price=price, price_source=source,
            liquidity_ok=ok, liquidity_reasons=reasons,
        ))

    payoff = compute_payoff(payoff_legs)
    liquidity_ok = all(l.liquidity_ok for l in legs)
    if not liquidity_ok:
        notes.append("one or more legs fail the configured liquidity gate (see per-leg liquidity_reasons)")

    return StrategyCandidate(
        strategy=strategy, underlying=summary.underlying, expiry=summary.expiry, supported=True,
        legs=tuple(legs), payoff=payoff, liquidity_ok=liquidity_ok, generation_notes=tuple(notes),
    )


def generate_candidates(
    strategies: list[StrategyType], summary: MarketStructureSummary, config: CandidateGenerationConfig, *, limit: int,
) -> list[StrategyCandidate]:
    """Section 19: bounded shortlist -- generates for every requested
    strategy but returns at most ``limit`` (supported candidates take
    priority over unsupported ones in the returned shortlist)."""
    generated = [generate_candidate(s, summary, config) for s in strategies]
    supported = [c for c in generated if c.supported]
    unsupported = [c for c in generated if not c.supported]
    return (supported + unsupported)[:limit]
