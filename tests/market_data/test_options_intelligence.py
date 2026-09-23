"""Phase 9 -- synthetic, hand-verifiable tests for the deterministic
Options Intelligence math (Section 55). No live Breeze data is used here;
every input is a fixed synthetic chain whose expected output was
independently hand-calculated (see comments)."""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from trading.market_data.options_intelligence import (
    DataQuality,
    IVSource,
    Moneyness,
    OptionType,
    atm_strike,
    black_scholes_greeks,
    black_scholes_price,
    build_expiry_info,
    build_market_structure_summary,
    calculate_expected_move,
    calculate_iv_rank,
    calculate_max_pain,
    calculate_oi_analytics,
    calculate_pcr,
    calculate_support_resistance,
    classify_moneyness,
    implied_volatility,
    is_monthly_expiry,
    time_to_expiry_years,
)
from trading.market_data.schemas import OptionChain, OptionChainRow, OptionQuote

EXPIRY = date(2026, 9, 29)


def _quote(strike, ot, *, ltp=None, oi=None, oi_change=None, iv=None, volume=None, bid=None, ask=None):
    return OptionQuote.build(
        underlying="NIFTY", expiry=EXPIRY, strike=strike, option_type=ot,
        ltp=ltp, oi=oi, oi_change=oi_change, iv=iv, volume=volume, bid=bid, ask=ask,
    )


def _chain(rows, *, spot=25000, atm=25000, expiry=EXPIRY, generated_at=None):
    return OptionChain(
        underlying="NIFTY", expiry=expiry, spot=spot, atm_strike=atm,
        generated_at=generated_at or datetime.now(timezone.utc), rows=rows, provider="test",
    )


# --------------------------------------------------------------------------
# ATM / moneyness
# --------------------------------------------------------------------------
def test_atm_is_nearest_listed_strike():
    assert atm_strike([24800, 24900, 25000, 25100, 25200], 25030) == 25000


def test_atm_tie_breaks_to_lower_strike_deterministically():
    # spot exactly between 25000 and 25100 -> equidistant, must pick 25000 every time.
    assert atm_strike([24900, 25000, 25100], 25050) == 25000


@pytest.mark.parametrize(
    "strike,option_type,expected",
    [
        (24900, OptionType.CALL, Moneyness.ITM),   # spot 25000 > strike -> call ITM
        (25000, OptionType.CALL, Moneyness.ATM),
        (25100, OptionType.CALL, Moneyness.OTM),
        (24900, OptionType.PUT, Moneyness.OTM),
        (25000, OptionType.PUT, Moneyness.ATM),
        (25100, OptionType.PUT, Moneyness.ITM),
    ],
)
def test_moneyness_classification(strike, option_type, expected):
    assert classify_moneyness(strike, 25000, option_type, atm=25000) == expected


# --------------------------------------------------------------------------
# PCR
# --------------------------------------------------------------------------
def test_oi_pcr_hand_calculated():
    rows = [
        OptionChainRow(strike=25000, call=_quote(25000, "CE", oi=1000), put=_quote(25000, "PE", oi=1500)),
        OptionChainRow(strike=25100, call=_quote(25100, "CE", oi=2000), put=_quote(25100, "PE", oi=2500)),
    ]
    # total call OI = 3000, total put OI = 4000 -> PCR = 4000/3000 = 1.3333
    result = calculate_pcr(_chain(rows))
    assert result.oi_pcr == pytest.approx(1.3333, abs=1e-4)


def test_volume_pcr_is_separate_from_oi_pcr():
    rows = [
        OptionChainRow(
            strike=25000,
            call=_quote(25000, "CE", oi=1000, volume=500),
            put=_quote(25000, "PE", oi=4000, volume=100),
        ),
    ]
    result = calculate_pcr(_chain(rows))
    assert result.oi_pcr == 4.0
    assert result.volume_pcr == pytest.approx(0.2)
    assert result.oi_pcr != result.volume_pcr


def test_pcr_handles_zero_denominator_safely():
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", oi=0), put=_quote(25000, "PE", oi=500))]
    result = calculate_pcr(_chain(rows))
    assert result.oi_pcr is None  # never a ZeroDivisionError, never a fabricated number


def test_pcr_none_when_no_oi_data_at_all():
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE"), put=_quote(25000, "PE"))]
    result = calculate_pcr(_chain(rows))
    assert result.oi_pcr is None and result.volume_pcr is None


# --------------------------------------------------------------------------
# Max Pain -- hand-calculated synthetic chain (see derivation below)
# --------------------------------------------------------------------------
def test_max_pain_hand_calculated():
    # Strikes: 24900 (call_oi=1000, put_oi=2000)
    #          25000 (call_oi=5000, put_oi=4000)
    #          25100 (call_oi=3000, put_oi=6000)
    #
    # Aggregate payout at each candidate settlement strike (sum over all
    # strikes of max(candidate-strike,0)*call_oi + max(strike-candidate,0)*put_oi):
    #   candidate=24900: puts only ITM -> 100*4000 + 200*6000 = 1,600,000
    #   candidate=25000: calls: 100*1000=100,000; puts: 100*6000=600,000 -> 700,000
    #   candidate=25100: calls: 200*1000 + 100*5000 = 700,000; puts: 0 -> 700,000
    # Minimum payout (700,000) is tied between 25000 and 25100 -> lower strike wins.
    rows = [
        OptionChainRow(strike=24900, call=_quote(24900, "CE", oi=1000), put=_quote(24900, "PE", oi=2000)),
        OptionChainRow(strike=25000, call=_quote(25000, "CE", oi=5000), put=_quote(25000, "PE", oi=4000)),
        OptionChainRow(strike=25100, call=_quote(25100, "CE", oi=3000), put=_quote(25100, "PE", oi=6000)),
    ]
    result = calculate_max_pain(_chain(rows))
    payouts = dict(result.payouts)
    assert payouts[24900] == pytest.approx(1_600_000)
    assert payouts[25000] == pytest.approx(700_000)
    assert payouts[25100] == pytest.approx(700_000)
    assert result.max_pain_strike == 25000  # tie resolved to the lower strike


def test_max_pain_empty_chain_returns_none():
    result = calculate_max_pain(_chain([]))
    assert result.max_pain_strike is None
    assert result.payouts == ()


def test_max_pain_missing_oi_treated_as_zero_not_fabricated():
    rows = [
        OptionChainRow(strike=25000, call=_quote(25000, "CE", oi=None), put=_quote(25000, "PE", oi=1000)),
        OptionChainRow(strike=25100, call=_quote(25100, "CE", oi=500), put=_quote(25100, "PE", oi=None)),
    ]
    result = calculate_max_pain(_chain(rows))
    assert result.max_pain_strike is not None  # still computes, treating missing OI as 0 contribution


# --------------------------------------------------------------------------
# OI analytics / support-resistance
# --------------------------------------------------------------------------
def test_oi_analytics_top_strikes_and_concentration():
    rows = [
        OptionChainRow(strike=24900, call=_quote(24900, "CE", oi=1000), put=_quote(24900, "PE", oi=9000)),
        OptionChainRow(strike=25000, call=_quote(25000, "CE", oi=8000), put=_quote(25000, "PE", oi=2000)),
        OptionChainRow(strike=25100, call=_quote(25100, "CE", oi=1000), put=_quote(25100, "PE", oi=1000)),
    ]
    oi = calculate_oi_analytics(_chain(rows), top_n=1)
    assert oi.total_call_oi == 10000
    assert oi.total_put_oi == 12000
    assert oi.top_call_oi_strikes == ((25000, 8000),)
    assert oi.top_put_oi_strikes == ((24900, 9000),)
    assert oi.call_oi_concentration == pytest.approx(0.8)

    sr = calculate_support_resistance(oi, top_n=1)
    assert sr.oi_resistance == (25000,)  # highest call OI -> resistance
    assert sr.oi_support == (24900,)     # highest put OI -> support


def test_oi_change_kept_distinct_from_absolute_oi():
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", oi=5000, oi_change=250), put=_quote(25000, "PE", oi=3000, oi_change=-100))]
    oi = calculate_oi_analytics(_chain(rows))
    assert oi.total_call_oi == 5000 and oi.total_call_oi_change == 250
    assert oi.total_put_oi == 3000 and oi.total_put_oi_change == -100


# --------------------------------------------------------------------------
# Expected move
# --------------------------------------------------------------------------
def test_expected_move_atm_straddle():
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=180.0), put=_quote(25000, "PE", ltp=130.0))]
    result = calculate_expected_move(_chain(rows, atm=25000), atm=25000)
    assert result.expected_move == pytest.approx(310.0)
    assert result.method == "ATM_STRADDLE"


def test_expected_move_none_when_a_leg_missing():
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=180.0), put=_quote(25000, "PE", ltp=None))]
    result = calculate_expected_move(_chain(rows, atm=25000), atm=25000)
    assert result.expected_move is None


# --------------------------------------------------------------------------
# Black-Scholes IV solver + Greeks
# --------------------------------------------------------------------------
def test_iv_solver_round_trips_a_known_price():
    spot, strike, t, r, sigma = 25000.0, 25000.0, 0.05, 0.065, 0.15
    price = black_scholes_price(spot, strike, t, r, sigma, OptionType.CALL)
    recovered = implied_volatility(price, spot, strike, t, r, OptionType.CALL)
    assert recovered == pytest.approx(sigma, abs=1e-4)


def test_iv_solver_round_trips_put():
    spot, strike, t, r, sigma = 25000.0, 24800.0, 0.08, 0.065, 0.22
    price = black_scholes_price(spot, strike, t, r, sigma, OptionType.PUT)
    recovered = implied_volatility(price, spot, strike, t, r, OptionType.PUT)
    assert recovered == pytest.approx(sigma, abs=1e-4)


def test_iv_solver_returns_none_for_price_below_intrinsic():
    # A call struck at 20000 with spot 25000 has intrinsic value >= 5000;
    # quoting it at 10 is not achievable at any positive volatility.
    result = implied_volatility(10.0, 25000.0, 20000.0, 0.05, 0.065, OptionType.CALL)
    assert result is None


def test_iv_solver_handles_zero_time_gracefully():
    assert implied_volatility(100.0, 25000.0, 25000.0, 0.0, 0.065, OptionType.CALL) is None


def test_greeks_call_delta_between_zero_and_one():
    g = black_scholes_greeks(25000, 25000, 0.05, 0.065, 0.15, OptionType.CALL)
    assert 0.0 < g.delta < 1.0
    assert g.gamma > 0
    assert g.vega_per_1pct > 0


def test_greeks_put_delta_between_minus_one_and_zero():
    g = black_scholes_greeks(25000, 25000, 0.05, 0.065, 0.15, OptionType.PUT)
    assert -1.0 < g.delta < 0.0


def test_greeks_theta_is_per_day_not_per_year():
    g_day = black_scholes_greeks(25000, 25000, 0.05, 0.065, 0.15, OptionType.CALL)
    # An ATM option's theta should be a small daily decay (a few points/day),
    # not the much larger raw annualized figure -- sanity bound catches a
    # units mistake (e.g. forgetting the /365).
    assert -50 < g_day.theta_per_day < 0


def test_deep_itm_call_delta_approaches_one():
    g = black_scholes_greeks(25000, 20000, 0.1, 0.065, 0.15, OptionType.CALL)
    assert g.delta > 0.95


# --------------------------------------------------------------------------
# Time to expiry / expiry classification
# --------------------------------------------------------------------------
def test_time_to_expiry_uses_settlement_cutoff_not_naive_dte():
    # Requesting at 09:15 IST (03:45 UTC) on expiry day itself should yield
    # a small POSITIVE fraction of a day remaining (until 15:30 IST), not
    # zero (a naive whole-day DTE/365 would floor this to 0).
    as_of = datetime(2026, 9, 29, 3, 45, tzinfo=timezone.utc)
    t = time_to_expiry_years(as_of, date(2026, 9, 29))
    assert t > 0
    assert t < (1 / 365)  # well under one full day


def test_time_to_expiry_is_zero_after_settlement():
    as_of = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)  # after 15:30 IST close
    assert time_to_expiry_years(as_of, date(2026, 9, 29)) == 0.0


def test_is_monthly_expiry_is_last_in_calendar_month_not_a_weekday_rule():
    expiries = [date(2026, 9, 3), date(2026, 9, 10), date(2026, 9, 17), date(2026, 9, 24), date(2026, 10, 1)]
    assert is_monthly_expiry(date(2026, 9, 24), expiries) is True
    assert is_monthly_expiry(date(2026, 9, 17), expiries) is False


def test_expiry_info_calendar_dte():
    as_of = datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc)
    info = build_expiry_info(date(2026, 9, 29), [date(2026, 9, 29)], as_of=as_of)
    assert info.days_to_expiry_calendar == 7
    assert info.is_nearest is True


# --------------------------------------------------------------------------
# IV Rank / Percentile -- must not fabricate from a single snapshot
# --------------------------------------------------------------------------
def test_iv_rank_not_available_with_insufficient_history():
    result = calculate_iv_rank(0.15, historical_iv=[0.12, 0.14, 0.16])
    assert result.status == "NOT_AVAILABLE"
    assert result.iv_rank is None and result.iv_percentile is None
    assert "insufficient historical IV history" in result.reason


def test_iv_rank_available_with_sufficient_history():
    history = [0.10 + 0.005 * i for i in range(25)]  # 25 points, 0.10..0.22
    result = calculate_iv_rank(0.16, historical_iv=history)
    assert result.status == "AVAILABLE"
    assert result.iv_rank is not None and result.iv_percentile is not None
    assert 0 <= result.iv_rank <= 100


def test_iv_rank_not_available_when_current_iv_missing():
    result = calculate_iv_rank(None, historical_iv=[0.1] * 30)
    assert result.status == "NOT_AVAILABLE"
    assert "current ATM IV unavailable" in result.reason


# --------------------------------------------------------------------------
# Full snapshot assembly
# --------------------------------------------------------------------------
def test_market_structure_summary_is_deterministic_no_llm():
    rows = [
        OptionChainRow(strike=24900, call=_quote(24900, "CE", ltp=250, oi=1000), put=_quote(24900, "PE", ltp=50, oi=2000)),
        OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=150, oi=5000), put=_quote(25000, "PE", ltp=100, oi=4000)),
        OptionChainRow(strike=25100, call=_quote(25100, "CE", ltp=80, oi=3000), put=_quote(25100, "PE", ltp=180, oi=6000)),
    ]
    chain = _chain(rows)
    summary1 = build_market_structure_summary(
        chain, as_of=None, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )
    summary2 = build_market_structure_summary(
        chain, as_of=None, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )
    assert summary1.max_pain.max_pain_strike == summary2.max_pain.max_pain_strike
    assert summary1.pcr.oi_pcr == summary2.pcr.oi_pcr
    assert summary1.atm_iv == summary2.atm_iv


def test_market_structure_summary_never_fabricates_iv_when_price_missing():
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=None, oi=100), put=_quote(25000, "PE", ltp=None, oi=100))]
    summary = build_market_structure_summary(
        _chain(rows), as_of=None, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )
    assert summary.atm_iv is None
    assert summary.atm_iv_source == IVSource.UNAVAILABLE


def test_market_structure_summary_prefers_provider_iv_over_calculated():
    rows = [OptionChainRow(strike=25000, call=_quote(25000, "CE", ltp=150, iv=0.20), put=_quote(25000, "PE", ltp=100))]
    summary = build_market_structure_summary(
        _chain(rows), as_of=None, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )
    assert summary.atm_iv == 0.20
    assert summary.atm_iv_source == IVSource.PROVIDER
