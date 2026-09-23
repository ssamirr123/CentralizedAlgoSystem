"""
Phase 10 (Section 25/26) -- deterministic, code-only payoff engine.

An option structure's expiration P&L is piecewise-LINEAR in the
underlying price, with breakpoints exactly at the legs' strikes. Rather
than hand-writing a max-profit/max-loss/breakeven FORMULA per strategy
(risking a subtly wrong formula for some leg combination), this engine
evaluates the exact piecewise-linear function itself:

  * the extreme value of a piecewise-linear function over an interval
    occurs at a breakpoint or at +/-infinity (S floor is 0, since an
    underlying price cannot go negative);
  * two well-separated evaluation points on the far side of the largest
    breakpoint reveal that segment's exact slope, which tells us whether
    profit/loss is bounded or unbounded on the upside;
  * breakevens are found by exact linear interpolation between
    consecutive breakpoints (or extrapolation on the unbounded segment
    using its known slope) -- exact because the function truly is linear
    there, not an approximation.

This is a single, uniform algorithm that works for every leg
combination -- no per-strategy formula table to get wrong, and it never
mislabels an unbounded tail as a finite number (Section 59: "Do not mark
mathematically undefined metrics as finite").
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.ai_options_research.strategy_templates import Side
from trading.market_data.options_intelligence import OptionType

_FAR_MULTIPLE = 4.0  # how far beyond the highest strike to probe the tail slope
_EPS = 1e-9


@dataclass(frozen=True)
class ResolvedLeg:
    side: Side
    option_type: OptionType
    strike: float
    price: float | None       # the estimated fill price used for this leg (Section 26)
    price_source: str         # "bid" | "ask" | "ltp" | "mid" -- which field the price came from
    liquidity_ok: bool
    liquidity_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PayoffPoint:
    underlying_price: float
    pnl: float


@dataclass(frozen=True)
class PayoffResult:
    net_premium: float | None       # positive = net credit received, negative = net debit paid
    max_profit: float | None        # positive magnitude; None if unbounded or unpriced
    max_profit_unbounded: bool
    max_loss: float | None          # positive magnitude; None if unbounded or unpriced
    max_loss_unbounded: bool
    breakevens: tuple[float, ...]
    pricing_method: str
    payoff_curve: tuple[PayoffPoint, ...]
    priced: bool                    # False if any leg has no usable price -- P&L figures are None
    detail: str | None = None


PRICING_METHOD = "short legs priced at bid (fallback: LTP); long legs priced at ask (fallback: LTP) -- a research ESTIMATE, never a guaranteed fill"


def price_leg(side: Side, *, bid: float | None, ask: float | None, ltp: float | None) -> tuple[float | None, str]:
    """Section 26: short legs -> bid (conservative estimate of what you'd
    actually receive); long legs -> ask (conservative estimate of what
    you'd actually pay). Falls back to LTP when bid/ask is unavailable,
    never fabricates a price when none of the three is present."""
    if side == Side.SELL:
        if bid is not None:
            return bid, "bid"
    else:
        if ask is not None:
            return ask, "ask"
    if ltp is not None:
        return ltp, "ltp"
    return None, "unavailable"


def _leg_payoff(leg: ResolvedLeg, s: float) -> float:
    intrinsic = max(s - leg.strike, 0.0) if leg.option_type == OptionType.CALL else max(leg.strike - s, 0.0)
    sign = 1.0 if leg.side == Side.BUY else -1.0
    premium_term = -(leg.price or 0.0) if leg.side == Side.BUY else (leg.price or 0.0)
    return sign * intrinsic + premium_term


def _total_pnl(legs: list[ResolvedLeg], s: float) -> float:
    return sum(_leg_payoff(leg, s) for leg in legs)


def compute_payoff(legs: list[ResolvedLeg], *, curve_points: int = 61) -> PayoffResult:
    if not legs:
        return PayoffResult(
            net_premium=None, max_profit=None, max_profit_unbounded=False,
            max_loss=None, max_loss_unbounded=False, breakevens=(), pricing_method=PRICING_METHOD,
            payoff_curve=(), priced=False, detail="no legs",
        )

    priced = all(leg.price is not None for leg in legs)
    net_premium = None
    if priced:
        net_premium = sum((leg.price if leg.side == Side.SELL else -leg.price) for leg in legs)  # type: ignore[operator]

    if not priced:
        return PayoffResult(
            net_premium=None, max_profit=None, max_profit_unbounded=False,
            max_loss=None, max_loss_unbounded=False, breakevens=(), pricing_method=PRICING_METHOD,
            payoff_curve=(), priced=False, detail="one or more legs have no usable price (bid/ask/ltp all missing)",
        )

    strikes = sorted({leg.strike for leg in legs})
    lo, hi = strikes[0], strikes[-1]
    far = hi + max(_FAR_MULTIPLE * (hi - lo), _FAR_MULTIPLE * hi, 1000.0)

    # Slope of the segment beyond the highest strike -- exact since the
    # payoff is linear there (two points on the same line fully determine it).
    pnl_hi = _total_pnl(legs, hi)
    pnl_far = _total_pnl(legs, far)
    slope_right = (pnl_far - pnl_hi) / (far - hi)

    max_profit_unbounded = slope_right > _EPS
    max_loss_unbounded = slope_right < -_EPS

    # Candidate extreme values: every breakpoint, plus S=0 (the true floor
    # -- an underlying price cannot go negative, so no "left tail" probe
    # is needed) and, when the right tail is flat, the far point too.
    eval_points = [0.0] + strikes
    if not max_profit_unbounded and not max_loss_unbounded:
        eval_points.append(far)
    pnls = [(p, _total_pnl(legs, p)) for p in eval_points]

    max_profit = None if max_profit_unbounded else max(v for _, v in pnls)
    max_loss = None if max_loss_unbounded else -min(v for _, v in pnls)
    if max_profit is not None:
        max_profit = round(max_profit, 4)
    if max_loss is not None:
        max_loss = round(max(max_loss, 0.0), 4)

    breakevens = _find_breakevens(legs, strikes, far, slope_right)

    curve = _build_curve(legs, lo, hi, curve_points)

    return PayoffResult(
        net_premium=round(net_premium, 4), max_profit=max_profit, max_profit_unbounded=max_profit_unbounded,
        max_loss=max_loss, max_loss_unbounded=max_loss_unbounded, breakevens=breakevens,
        pricing_method=PRICING_METHOD, payoff_curve=curve, priced=True,
    )


def _find_breakevens(legs: list[ResolvedLeg], strikes: list[float], far: float, slope_right: float) -> tuple[float, ...]:
    points = [0.0] + strikes + [far]
    values = [_total_pnl(legs, p) for p in points]
    breakevens: list[float] = []
    for i in range(len(points) - 1):
        p0, p1 = points[i], points[i + 1]
        v0, v1 = values[i], values[i + 1]
        if v0 == 0.0:
            breakevens.append(round(p0, 4))
            continue
        if (v0 < 0) != (v1 < 0) and v1 != v0:
            # Linear interpolation -- exact, since the segment is truly linear.
            crossing = p0 + (0.0 - v0) * (p1 - p0) / (v1 - v0)
            breakevens.append(round(crossing, 4))
    if values[-1] == 0.0 and (not breakevens or breakevens[-1] != round(points[-1], 4)):
        breakevens.append(round(points[-1], 4))
    # De-dup while preserving order (adjacent segments can share an exact-zero breakpoint).
    seen: set[float] = set()
    result = []
    for b in breakevens:
        if b not in seen:
            seen.add(b)
            result.append(b)
    return tuple(sorted(result))


def _build_curve(legs: list[ResolvedLeg], lo: float, hi: float, n: int) -> tuple[PayoffPoint, ...]:
    span = max(hi - lo, 1.0)
    start = max(lo - span, 0.0)
    end = hi + span
    step = (end - start) / max(n - 1, 1)
    return tuple(PayoffPoint(round(start + i * step, 2), round(_total_pnl(legs, start + i * step), 4)) for i in range(n))
