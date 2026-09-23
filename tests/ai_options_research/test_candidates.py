"""Phase 10 (Section 58/60) -- deterministic candidate generation tests
against a synthetic chain with known expected strikes."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading.ai_options_research.candidates import CandidateGenerationConfig, generate_candidate, generate_candidates
from trading.ai_options_research.strategy_templates import Side, StrategyType
from trading.market_data.options_intelligence import OptionType, build_market_structure_summary
from trading.market_data.schemas import OptionChain, OptionChainRow, OptionQuote

EXPIRY = date(2026, 9, 29)
AS_OF = datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc)


def _quote(strike, ot, *, ltp, bid=None, ask=None, volume=None, oi=None):
    return OptionQuote.build(
        underlying="NIFTY", expiry=EXPIRY, strike=strike, option_type=ot,
        ltp=ltp, bid=bid if bid is not None else ltp - 0.5, ask=ask if ask is not None else ltp + 0.5,
        volume=volume, oi=oi,
    )


def _synthetic_summary(spot=25000, wide=True):
    """A wide synthetic chain (24000-26000, step 100) with real prices
    priced approximately via a simple decay so every strike gets a
    computable IV/delta -- realistic enough for deterministic strike
    selection tests, not used for math-correctness assertions (that's
    test_options_intelligence.py's job)."""
    rows = []
    strikes = range(24000, 26001, 100) if wide else range(24800, 25201, 100)
    for k in strikes:
        call_intrinsic = max(spot - k, 0)
        put_intrinsic = max(k - spot, 0)
        call_ltp = round(call_intrinsic + max(150 - abs(spot - k) * 0.15, 5), 2)
        put_ltp = round(put_intrinsic + max(150 - abs(spot - k) * 0.15, 5), 2)
        rows.append(OptionChainRow(
            strike=float(k),
            call=_quote(k, "CE", ltp=call_ltp, volume=10000, oi=50000),
            put=_quote(k, "PE", ltp=put_ltp, volume=10000, oi=50000),
        ))
    chain = OptionChain(underlying="NIFTY", expiry=EXPIRY, spot=float(spot), atm_strike=float(spot), generated_at=AS_OF, rows=rows, provider="test")
    return build_market_structure_summary(
        chain, as_of=AS_OF, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )


def test_iron_condor_uses_only_real_strikes():
    summary = _synthetic_summary()
    real_strikes = {r.strike for r in summary.rows}
    config = CandidateGenerationConfig(wing_width=300)
    candidate = generate_candidate(StrategyType.IRON_CONDOR, summary, config)
    assert candidate.supported
    assert len(candidate.legs) == 4
    for leg in candidate.legs:
        assert leg.strike in real_strikes  # never a hallucinated/invented strike


def test_iron_condor_leg_ordering_and_sides():
    summary = _synthetic_summary()
    config = CandidateGenerationConfig(wing_width=300)
    candidate = generate_candidate(StrategyType.IRON_CONDOR, summary, config)
    sides_types = [(l.side, l.option_type) for l in candidate.legs]
    assert sides_types == [
        (Side.SELL, OptionType.PUT), (Side.BUY, OptionType.PUT),
        (Side.SELL, OptionType.CALL), (Side.BUY, OptionType.CALL),
    ]
    put_short, put_long, call_short, call_long = candidate.legs
    assert put_long.strike < put_short.strike < 25000 < call_short.strike < call_long.strike


def test_long_call_uses_atm_strike():
    summary = _synthetic_summary()
    candidate = generate_candidate(StrategyType.LONG_CALL, summary, CandidateGenerationConfig())
    assert candidate.supported
    assert candidate.legs[0].strike == summary.atm_strike


def test_calendar_spread_marked_unsupported_not_invented():
    summary = _synthetic_summary()
    candidate = generate_candidate(StrategyType.CALENDAR_SPREAD, summary, CandidateGenerationConfig())
    assert candidate.supported is False
    assert candidate.legs == ()
    assert "single-expiry" in candidate.unsupported_reason


def test_no_chain_data_is_unsupported_not_crashing():
    chain = OptionChain(underlying="NIFTY", expiry=EXPIRY, spot=None, atm_strike=None, generated_at=AS_OF, rows=[], provider="test")
    summary = build_market_structure_summary(
        chain, as_of=AS_OF, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )
    candidate = generate_candidate(StrategyType.IRON_CONDOR, summary, CandidateGenerationConfig())
    assert candidate.supported is False


def test_generate_candidates_bounded_by_limit():
    summary = _synthetic_summary()
    strategies = [StrategyType.IRON_CONDOR, StrategyType.IRON_FLY, StrategyType.SHORT_STRANGLE, StrategyType.LONG_CALL]
    result = generate_candidates(strategies, summary, CandidateGenerationConfig(wing_width=300), limit=2)
    assert len(result) == 2


def test_unsupported_strategies_deprioritized_in_shortlist():
    summary = _synthetic_summary()
    strategies = [StrategyType.CALENDAR_SPREAD, StrategyType.LONG_CALL]
    result = generate_candidates(strategies, summary, CandidateGenerationConfig(), limit=1)
    assert result[0].strategy == StrategyType.LONG_CALL  # supported prioritized over unsupported


# --------------------------------------------------------------------------
# Liquidity gate (Section 60)
# --------------------------------------------------------------------------
def test_illiquid_leg_flagged_not_silently_dropped():
    rows = []
    for k in range(24800, 25201, 100):
        illiquid = k == 25000
        rows.append(OptionChainRow(
            strike=float(k),
            call=_quote(k, "CE", ltp=100, volume=0 if illiquid else 5000, oi=0 if illiquid else 20000),
            put=_quote(k, "PE", ltp=100, volume=0 if illiquid else 5000, oi=0 if illiquid else 20000),
        ))
    chain = OptionChain(underlying="NIFTY", expiry=EXPIRY, spot=25000.0, atm_strike=25000.0, generated_at=AS_OF, rows=rows, provider="test")
    summary = build_market_structure_summary(
        chain, as_of=AS_OF, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )
    config = CandidateGenerationConfig(minimum_volume=100, minimum_oi=100)
    candidate = generate_candidate(StrategyType.LONG_CALL, summary, config)
    assert candidate.supported
    assert candidate.liquidity_ok is False
    assert any("volume" in r or "interest" in r for r in candidate.legs[0].liquidity_reasons)


def test_liquid_legs_pass_gate():
    summary = _synthetic_summary()
    config = CandidateGenerationConfig(minimum_volume=100, minimum_oi=100)
    candidate = generate_candidate(StrategyType.LONG_CALL, summary, config)
    assert candidate.liquidity_ok is True


def test_max_bid_ask_spread_gate():
    rows = [OptionChainRow(
        strike=25000.0,
        call=_quote(25000, "CE", ltp=100, bid=90, ask=110, volume=5000, oi=20000),  # 20pt spread
        put=_quote(25000, "PE", ltp=100, volume=5000, oi=20000),
    )]
    chain = OptionChain(underlying="NIFTY", expiry=EXPIRY, spot=25000.0, atm_strike=25000.0, generated_at=AS_OF, rows=rows, provider="test")
    summary = build_market_structure_summary(
        chain, as_of=AS_OF, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )
    config = CandidateGenerationConfig(maximum_bid_ask_spread=5.0)
    candidate = generate_candidate(StrategyType.LONG_CALL, summary, config)
    assert candidate.liquidity_ok is False
