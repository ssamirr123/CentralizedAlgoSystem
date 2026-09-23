"""
Phase 9 -- NIFTY Options Intelligence Engine (deterministic calculations).

Pure functions/dataclasses only. No I/O, no Breeze, no database, no LLM.
Everything here operates on an already-fetched ``OptionChain`` (see
``trading.market_data.schemas``) and returns structured, inspectable
results. Acquisition/persistence/orchestration lives in
``options_service.py``; this module never calls a provider (Section 47:
"do NOT ask an LLM to calculate PCR/Max Pain/Greeks/IV/ATM/DTE/expected
move/OI totals" -- the corollary enforced here is that none of it is
ambiguous or fuzzy either: every number below is arithmetic on real,
already-normalized field values, and a missing input yields ``None``,
never a fabricated number.

Canonical option-type representation (Section 5): CALL/PUT internally;
the rest of the codebase (``trading.market_data.*``) uses "CE"/"PE" as
its own established convention (an NSE contract convention, not a
provider-specific one) -- ``to_ce_pe``/``from_ce_pe`` translate at this
module's boundary so callers of this module never need to think in CE/PE.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum

from trading.market_data.schemas import OptionChain, OptionQuote

CALCULATION_VERSION = "phase9.1"

# NSE index-options settlement cutoff, matching Phase 7's NSE calendar
# (Asia/Kolkata 15:30 close) -- Section 24 requires an explicit, documented
# time-to-expiry method rather than a naive DTE/365 whole-day approximation.
_SETTLEMENT_TIME = time(15, 30)


# --------------------------------------------------------------------------
# Canonical enums
# --------------------------------------------------------------------------
class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


def to_ce_pe(option_type: OptionType) -> str:
    return "CE" if option_type == OptionType.CALL else "PE"


def from_ce_pe(ce_pe: str) -> OptionType:
    ce_pe = ce_pe.strip().upper()
    if ce_pe == "CE":
        return OptionType.CALL
    if ce_pe == "PE":
        return OptionType.PUT
    raise ValueError(f"Not a CE/PE option type: {ce_pe!r}")


class Moneyness(str, Enum):
    ITM = "ITM"
    ATM = "ATM"
    OTM = "OTM"


class IVSource(str, Enum):
    PROVIDER = "PROVIDER"
    CALCULATED = "CALCULATED"
    UNAVAILABLE = "UNAVAILABLE"


class DataQuality(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    MISSING = "MISSING"


# Section 32: acceptable spot-vs-chain timestamp gap before analytics are
# flagged STALE/MISALIGNED rather than silently computed against a
# materially newer/older price.
TIMESTAMP_ALIGNMENT_TOLERANCE_SECONDS = 5 * 60


# --------------------------------------------------------------------------
# Result dataclasses
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ExpiryInfo:
    expiry: date
    days_to_expiry_calendar: int
    days_to_expiry_trading: int | None  # None if the NSE calendar has no coverage for this range
    is_nearest: bool
    is_monthly: bool


@dataclass(frozen=True)
class GreeksResult:
    """Units (Section 26): delta/gamma/rho are the standard Black-Scholes
    per-unit-underlying figures. theta_per_day is the *annualized* BS theta
    divided by 365 (price decay per calendar day, not per trading day).
    vega_per_1pct is BS vega (per 1.0 = 100 vol points) divided by 100, i.e.
    the price change for a 1 percentage point (0.01) change in IV."""

    delta: float | None
    gamma: float | None
    theta_per_day: float | None
    vega_per_1pct: float | None
    rho: float | None = None


@dataclass(frozen=True)
class OptionAnalyticsRow:
    strike: float
    option_type: OptionType
    quote: OptionQuote | None
    moneyness: Moneyness | None
    iv: float | None
    iv_source: IVSource
    greeks: GreeksResult | None


@dataclass(frozen=True)
class OIAnalytics:
    total_call_oi: int
    total_put_oi: int
    total_call_oi_change: int | None
    total_put_oi_change: int | None
    top_call_oi_strikes: tuple[tuple[float, int], ...]  # (strike, oi) desc
    top_put_oi_strikes: tuple[tuple[float, int], ...]
    call_oi_concentration: float | None  # top strike's OI / total call OI
    put_oi_concentration: float | None


@dataclass(frozen=True)
class PCRResult:
    oi_pcr: float | None
    volume_pcr: float | None


@dataclass(frozen=True)
class MaxPainResult:
    max_pain_strike: float | None
    payouts: tuple[tuple[float, float], ...]  # (candidate settlement strike, aggregate payout)


@dataclass(frozen=True)
class SupportResistance:
    oi_support: tuple[float, ...]     # highest Put-OI strikes (potential support)
    oi_resistance: tuple[float, ...]  # highest Call-OI strikes (potential resistance)


@dataclass(frozen=True)
class ExpectedMoveResult:
    expected_move: float | None
    method: str  # e.g. "ATM_STRADDLE"


@dataclass(frozen=True)
class IVRankResult:
    iv_rank: float | None
    iv_percentile: float | None
    status: str  # "AVAILABLE" | "NOT_AVAILABLE"
    reason: str | None = None


@dataclass(frozen=True)
class DataQualityReport:
    status: DataQuality
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provenance:
    underlying_source: str
    option_chain_source: str
    spot_timestamp: datetime | None
    chain_timestamp: datetime | None
    calculation_version: str
    risk_free_rate: float
    provider_call_count: int


@dataclass(frozen=True)
class MarketStructureSummary:
    underlying: str
    spot: float | None
    spot_timestamp: datetime | None
    expiry: date | None
    expiry_info: ExpiryInfo | None
    atm_strike: float | None
    oi: OIAnalytics | None
    pcr: PCRResult
    max_pain: MaxPainResult
    support_resistance: SupportResistance
    atm_iv: float | None
    atm_iv_source: IVSource
    expected_move: ExpectedMoveResult
    iv_rank: IVRankResult
    quality: DataQualityReport
    provenance: Provenance
    rows: tuple[OptionAnalyticsRow, ...]
    as_of: datetime | None  # None == live/current


# --------------------------------------------------------------------------
# ATM / moneyness
# --------------------------------------------------------------------------
def atm_strike(strikes: list[float], spot: float) -> float | None:
    """Deterministic ATM = valid listed strike nearest spot. Ties (equal
    distance above/below) resolve to the LOWER strike, deterministically."""
    if not strikes or spot is None:
        return None
    best = min(strikes, key=lambda s: (abs(s - spot), s))
    return best


def classify_moneyness(strike: float, spot: float | None, option_type: OptionType, atm: float | None) -> Moneyness | None:
    if spot is None:
        return None
    if atm is not None and strike == atm:
        return Moneyness.ATM
    if option_type == OptionType.CALL:
        return Moneyness.ITM if strike < spot else Moneyness.OTM
    return Moneyness.ITM if strike > spot else Moneyness.OTM


# --------------------------------------------------------------------------
# Expiry classification
# --------------------------------------------------------------------------
def settlement_datetime(expiry: date) -> datetime:
    """NSE index-options settlement cutoff, Asia/Kolkata 15:30 -- reuses
    the same session-close convention as Phase 7's calendar_nse module
    (kept dependency-free here; callers needing calendar-holiday awareness
    pass ``trading_days_in_range`` results in for days_to_expiry_trading)."""
    from zoneinfo import ZoneInfo

    return datetime.combine(expiry, _SETTLEMENT_TIME, tzinfo=ZoneInfo("Asia/Kolkata"))


def is_monthly_expiry(expiry: date, all_expiries: list[date]) -> bool:
    """The last listed expiry within its calendar month, among the
    provided (already-fetched) expiry list -- never a hardcoded weekday
    rule (Section 6 explicitly forbids assuming "Thursday")."""
    same_month = [e for e in all_expiries if e.year == expiry.year and e.month == expiry.month]
    return bool(same_month) and expiry == max(same_month)


def build_expiry_info(
    expiry: date,
    all_expiries: list[date],
    *,
    as_of: datetime,
    trading_days: list[date] | None = None,
) -> ExpiryInfo:
    as_of_date = as_of.date()
    calendar_dte = (expiry - as_of_date).days
    trading_dte = None
    if trading_days is not None:
        trading_dte = sum(1 for d in trading_days if as_of_date < d <= expiry)
    nearest = min(all_expiries, default=None, key=lambda e: (e < as_of_date, abs((e - as_of_date).days)))
    return ExpiryInfo(
        expiry=expiry,
        days_to_expiry_calendar=calendar_dte,
        days_to_expiry_trading=trading_dte,
        is_nearest=(nearest == expiry),
        is_monthly=is_monthly_expiry(expiry, all_expiries),
    )


def time_to_expiry_years(as_of: datetime, expiry: date) -> float:
    """ACT/365 with an explicit intraday time fraction to the 15:30 IST
    settlement cutoff -- NOT a naive whole-day DTE/365 (Section 24). A
    request made at 14:00 IST on expiry day correctly yields a small
    positive fraction of a day remaining, not zero and not a full day."""
    settle = settlement_datetime(expiry)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    delta = (settle - as_of).total_seconds()
    return max(delta, 0.0) / (365.0 * 24 * 3600)


# --------------------------------------------------------------------------
# OI analytics / PCR / Max Pain / support-resistance
# --------------------------------------------------------------------------
def _chain_pairs(chain: OptionChain) -> list[tuple[float, OptionQuote | None, OptionQuote | None]]:
    return [(row.strike, row.call, row.put) for row in chain.rows]


def calculate_oi_analytics(chain: OptionChain, *, top_n: int = 3) -> OIAnalytics:
    call_oi: dict[float, int] = {}
    put_oi: dict[float, int] = {}
    call_change = 0
    put_change = 0
    any_call_change = False
    any_put_change = False
    for strike, call, put in _chain_pairs(chain):
        if call is not None and call.oi is not None:
            call_oi[strike] = call.oi
            if call.oi_change is not None:
                call_change += call.oi_change
                any_call_change = True
        if put is not None and put.oi is not None:
            put_oi[strike] = put.oi
            if put.oi_change is not None:
                put_change += put.oi_change
                any_put_change = True

    total_call = sum(call_oi.values())
    total_put = sum(put_oi.values())
    top_calls = tuple(sorted(call_oi.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])
    top_puts = tuple(sorted(put_oi.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])

    call_concentration = (top_calls[0][1] / total_call) if (top_calls and total_call) else None
    put_concentration = (top_puts[0][1] / total_put) if (top_puts and total_put) else None

    return OIAnalytics(
        total_call_oi=total_call,
        total_put_oi=total_put,
        total_call_oi_change=call_change if any_call_change else None,
        total_put_oi_change=put_change if any_put_change else None,
        top_call_oi_strikes=top_calls,
        top_put_oi_strikes=top_puts,
        call_oi_concentration=call_concentration,
        put_oi_concentration=put_concentration,
    )


def calculate_support_resistance(oi: OIAnalytics, *, top_n: int = 2) -> SupportResistance:
    resistance = tuple(strike for strike, _ in oi.top_call_oi_strikes[:top_n])
    support = tuple(strike for strike, _ in oi.top_put_oi_strikes[:top_n])
    return SupportResistance(oi_support=support, oi_resistance=resistance)


def calculate_pcr(chain: OptionChain) -> PCRResult:
    """OI PCR = total put OI / total call OI. Volume PCR = total put
    volume / total call volume -- a SEPARATE metric (Section 19: "do not
    mix them"). Zero/absent denominators return None rather than raising
    or fabricating a divide-by-zero sentinel."""
    total_call_oi = total_put_oi = 0
    total_call_vol = total_put_vol = 0
    have_oi = have_vol = False
    for _, call, put in _chain_pairs(chain):
        if call is not None and call.oi is not None:
            total_call_oi += call.oi
            have_oi = True
        if put is not None and put.oi is not None:
            total_put_oi += put.oi
            have_oi = True
        if call is not None and call.volume is not None:
            total_call_vol += call.volume
            have_vol = True
        if put is not None and put.volume is not None:
            total_put_vol += put.volume
            have_vol = True

    oi_pcr = round(total_put_oi / total_call_oi, 4) if (have_oi and total_call_oi) else None
    vol_pcr = round(total_put_vol / total_call_vol, 4) if (have_vol and total_call_vol) else None
    return PCRResult(oi_pcr=oi_pcr, volume_pcr=vol_pcr)


def calculate_max_pain(chain: OptionChain) -> MaxPainResult:
    """For each candidate settlement strike, total intrinsic payout owed
    to option HOLDERS across the whole chain (writer's pain); the strike
    minimizing that aggregate payout is Max Pain -- the classic
    definition. Diagnostic payouts for every candidate are returned so the
    calculation can be independently verified (Section 20)."""
    strikes = sorted(row.strike for row in chain.rows)
    if not strikes:
        return MaxPainResult(max_pain_strike=None, payouts=())

    oi_by_strike: dict[float, tuple[int, int]] = {}
    for strike, call, put in _chain_pairs(chain):
        call_oi = call.oi if (call is not None and call.oi is not None) else 0
        put_oi = put.oi if (put is not None and put.oi is not None) else 0
        oi_by_strike[strike] = (call_oi, put_oi)

    payouts: list[tuple[float, float]] = []
    for candidate in strikes:
        total = 0.0
        for strike, (call_oi, put_oi) in oi_by_strike.items():
            total += max(candidate - strike, 0.0) * call_oi   # calls ITM at settlement=candidate
            total += max(strike - candidate, 0.0) * put_oi    # puts ITM at settlement=candidate
        payouts.append((candidate, total))

    min_payout = min(p for _, p in payouts)
    max_pain = min(s for s, p in payouts if p == min_payout)
    return MaxPainResult(max_pain_strike=max_pain, payouts=tuple(payouts))


# --------------------------------------------------------------------------
# Black-Scholes IV solver + Greeks
# --------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes_price(spot: float, strike: float, t: float, r: float, sigma: float, option_type: OptionType) -> float:
    """Standard Black-Scholes (no dividend yield / carry adjustment --
    Section 22 documented assumption: q=0). t is in years."""
    if t <= 0 or sigma <= 0:
        intrinsic = max(spot - strike, 0.0) if option_type == OptionType.CALL else max(strike - spot, 0.0)
        return intrinsic
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if option_type == OptionType.CALL:
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(
    option_price: float, spot: float, strike: float, t: float, r: float, option_type: OptionType,
    *, lo: float = 1e-4, hi: float = 5.0, tolerance: float = 1e-4, max_iterations: int = 100,
) -> float | None:
    """Bisection solver (robust, no derivative-blowup risk near expiry
    unlike Newton-Raphson) on Black-Scholes price. Returns None -- never a
    fabricated number -- when the price is outside any achievable BS
    price for this option (Section 22: "handle impossible prices
    gracefully"), e.g. below intrinsic value or non-positive inputs."""
    if option_price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    intrinsic = max(spot - strike, 0.0) if option_type == OptionType.CALL else max(strike - spot, 0.0)
    if option_price < intrinsic - tolerance:
        return None  # priced below intrinsic value -- not a valid BS price at any sigma

    price_lo = black_scholes_price(spot, strike, t, r, lo, option_type)
    price_hi = black_scholes_price(spot, strike, t, r, hi, option_type)
    if option_price < price_lo - tolerance or option_price > price_hi + tolerance:
        return None  # unreachable within the search bracket

    for _ in range(max_iterations):
        mid = (lo + hi) / 2.0
        price_mid = black_scholes_price(spot, strike, t, r, mid, option_type)
        if abs(price_mid - option_price) < tolerance:
            return round(mid, 6)
        if price_mid > option_price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2.0, 6)


def black_scholes_greeks(spot: float, strike: float, t: float, r: float, sigma: float, option_type: OptionType) -> GreeksResult:
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return GreeksResult(delta=None, gamma=None, theta_per_day=None, vega_per_1pct=None, rho=None)

    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)

    if option_type == OptionType.CALL:
        delta = _norm_cdf(d1)
        theta_annual = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t) - r * strike * math.exp(-r * t) * _norm_cdf(d2)
        )
        rho = strike * t * math.exp(-r * t) * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t) + r * strike * math.exp(-r * t) * _norm_cdf(-d2)
        )
        rho = -strike * t * math.exp(-r * t) * _norm_cdf(-d2) / 100.0

    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t  # per 1.0 (100 vol points) change in sigma

    return GreeksResult(
        delta=round(delta, 6),
        gamma=round(gamma, 8),
        theta_per_day=round(theta_annual / 365.0, 6),
        vega_per_1pct=round(vega / 100.0, 6),
        rho=round(rho, 6),
    )


# --------------------------------------------------------------------------
# Expected move
# --------------------------------------------------------------------------
def calculate_expected_move(chain: OptionChain, atm: float | None) -> ExpectedMoveResult:
    """ATM-straddle approximation: expected move ~= ATM call premium + ATM
    put premium (Section 29). Requires BOTH legs' LTP to be present."""
    if atm is None:
        return ExpectedMoveResult(expected_move=None, method="ATM_STRADDLE")
    row = next((r for r in chain.rows if r.strike == atm), None)
    if row is None or row.call is None or row.put is None or row.call.ltp is None or row.put.ltp is None:
        return ExpectedMoveResult(expected_move=None, method="ATM_STRADDLE")
    return ExpectedMoveResult(expected_move=round(row.call.ltp + row.put.ltp, 4), method="ATM_STRADDLE")


# --------------------------------------------------------------------------
# IV Rank / Percentile (Section 28)
# --------------------------------------------------------------------------
_IV_HISTORY_MIN_POINTS = 20  # a deliberately conservative floor; see final report


def calculate_iv_rank(current_iv: float | None, historical_iv: list[float]) -> IVRankResult:
    """Only returns a real number once enough historical ATM-IV
    observations have accumulated (Section 28: never fabricate from a
    single snapshot). Storage is designed so this naturally starts
    returning AVAILABLE once daily collection has run for a while --
    no schema change needed later."""
    if current_iv is None:
        return IVRankResult(iv_rank=None, iv_percentile=None, status="NOT_AVAILABLE", reason="current ATM IV unavailable")
    if len(historical_iv) < _IV_HISTORY_MIN_POINTS:
        return IVRankResult(
            iv_rank=None, iv_percentile=None, status="NOT_AVAILABLE",
            reason=f"insufficient historical IV history ({len(historical_iv)}/{_IV_HISTORY_MIN_POINTS} observations)",
        )
    lo, hi = min(historical_iv), max(historical_iv)
    rank = 0.0 if hi == lo else (current_iv - lo) / (hi - lo) * 100.0
    percentile = sum(1 for v in historical_iv if v <= current_iv) / len(historical_iv) * 100.0
    return IVRankResult(iv_rank=round(rank, 2), iv_percentile=round(percentile, 2), status="AVAILABLE")


# --------------------------------------------------------------------------
# Data quality
# --------------------------------------------------------------------------
def assess_data_quality(
    chain: OptionChain,
    *,
    spot_timestamp: datetime | None,
    chain_timestamp: datetime | None,
    now: datetime | None = None,
) -> DataQualityReport:
    reasons: list[str] = []
    if not chain.rows:
        return DataQualityReport(status=DataQuality.MISSING, reasons=("No strikes in chain",))

    missing_call = sum(1 for r in chain.rows if r.call is None)
    missing_put = sum(1 for r in chain.rows if r.put is None)
    missing_iv = sum(1 for r in chain.rows if (r.call is None or r.call.iv is None) or (r.put is None or r.put.iv is None))
    if missing_call:
        reasons.append(f"Call quote missing for {missing_call} strikes")
    if missing_put:
        reasons.append(f"Put quote missing for {missing_put} strikes")
    if missing_iv:
        reasons.append(f"IV missing for {missing_iv} strikes")
    if chain.spot is None:
        reasons.append("Spot price unavailable")

    stale = False
    if spot_timestamp is not None and chain_timestamp is not None:
        gap = abs((chain_timestamp - spot_timestamp).total_seconds())
        if gap > TIMESTAMP_ALIGNMENT_TOLERANCE_SECONDS:
            stale = True
            reasons.append(f"Spot/chain timestamps misaligned by {int(gap)}s (tolerance {TIMESTAMP_ALIGNMENT_TOLERANCE_SECONDS}s)")

    now = now or datetime.now(timezone.utc)
    if chain_timestamp is not None:
        age = (now - chain_timestamp).total_seconds()
        if age > TIMESTAMP_ALIGNMENT_TOLERANCE_SECONDS:
            stale = True
            reasons.append(f"Chain data stale by {int(age)}s")

    if chain.spot is None or (missing_call + missing_put) == 2 * len(chain.rows):
        status = DataQuality.MISSING
    elif stale:
        status = DataQuality.STALE
    elif missing_call or missing_put or missing_iv:
        status = DataQuality.PARTIAL
    else:
        status = DataQuality.COMPLETE
    return DataQualityReport(status=status, reasons=tuple(reasons))


# --------------------------------------------------------------------------
# Full snapshot assembly
# --------------------------------------------------------------------------
def build_market_structure_summary(
    chain: OptionChain,
    *,
    as_of: datetime | None,
    risk_free_rate: float,
    all_expiries: list[date] | None = None,
    trading_days: list[date] | None = None,
    underlying_source: str,
    option_chain_source: str,
    provider_call_count: int,
    historical_iv: list[float] | None = None,
) -> MarketStructureSummary:
    """Pure assembly: given a chain (already fetched -- live or
    historical) and inputs, compute every deterministic metric. Never
    calls a provider or a database."""
    effective_as_of = as_of or chain.generated_at
    atm = chain.atm_strike if chain.atm_strike is not None else atm_strike(
        [r.strike for r in chain.rows], chain.spot
    ) if chain.spot is not None else None

    t_years = time_to_expiry_years(effective_as_of, chain.expiry) if chain.expiry else 0.0

    rows: list[OptionAnalyticsRow] = []
    atm_iv: float | None = None
    atm_iv_source = IVSource.UNAVAILABLE
    for row in chain.rows:
        for ot, quote in ((OptionType.CALL, row.call), (OptionType.PUT, row.put)):
            moneyness = classify_moneyness(row.strike, chain.spot, ot, atm)
            iv: float | None = None
            iv_source = IVSource.UNAVAILABLE
            greeks: GreeksResult | None = None
            if quote is not None:
                if quote.iv is not None:
                    iv, iv_source = quote.iv, IVSource.PROVIDER
                elif quote.ltp is not None and chain.spot is not None and t_years > 0:
                    calc = implied_volatility(quote.ltp, chain.spot, row.strike, t_years, risk_free_rate, ot)
                    if calc is not None:
                        iv, iv_source = calc, IVSource.CALCULATED
                if iv is not None and chain.spot is not None and t_years > 0:
                    greeks = black_scholes_greeks(chain.spot, row.strike, t_years, risk_free_rate, iv, ot)
            rows.append(OptionAnalyticsRow(
                strike=row.strike, option_type=ot, quote=quote, moneyness=moneyness,
                iv=iv, iv_source=iv_source, greeks=greeks,
            ))
            if atm is not None and row.strike == atm and iv is not None:
                # Prefer the call leg's ATM IV if both present; otherwise whichever resolved.
                if atm_iv is None or ot == OptionType.CALL:
                    atm_iv, atm_iv_source = iv, iv_source

    oi = calculate_oi_analytics(chain)
    pcr = calculate_pcr(chain)
    max_pain = calculate_max_pain(chain)
    support_resistance = calculate_support_resistance(oi)
    expected_move = calculate_expected_move(chain, atm)
    expiry_info = (
        build_expiry_info(chain.expiry, all_expiries or [chain.expiry], as_of=effective_as_of, trading_days=trading_days)
        if chain.expiry else None
    )
    iv_rank = calculate_iv_rank(atm_iv, historical_iv or [])

    spot_ts = None
    quality = assess_data_quality(chain, spot_timestamp=spot_ts, chain_timestamp=chain.generated_at)

    provenance = Provenance(
        underlying_source=underlying_source,
        option_chain_source=option_chain_source,
        spot_timestamp=spot_ts,
        chain_timestamp=chain.generated_at,
        calculation_version=CALCULATION_VERSION,
        risk_free_rate=risk_free_rate,
        provider_call_count=provider_call_count,
    )

    return MarketStructureSummary(
        underlying=chain.underlying, spot=chain.spot, spot_timestamp=spot_ts,
        expiry=chain.expiry, expiry_info=expiry_info, atm_strike=atm,
        oi=oi, pcr=pcr, max_pain=max_pain, support_resistance=support_resistance,
        atm_iv=atm_iv, atm_iv_source=atm_iv_source, expected_move=expected_move,
        iv_rank=iv_rank, quality=quality, provenance=provenance,
        rows=tuple(rows), as_of=as_of,
    )
