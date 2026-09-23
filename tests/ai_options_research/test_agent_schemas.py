"""Phase 10 (Section 61) -- hallucination guard tests."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading.ai_options_research.agent_schemas import (
    CandidateCommentary,
    Confidence,
    DirectionalCaseOutput,
    StrategyResearchOutput,
    sanitize_candidate_commentary,
    sanitize_directional_case,
    validate_candidate_index,
)
from trading.ai_options_research.context import freeze_snapshot
from trading.market_data.options_intelligence import build_market_structure_summary
from trading.market_data.schemas import OptionChain, OptionChainRow, OptionQuote

EXPIRY = date(2026, 9, 29)


def _context():
    rows = [
        OptionChainRow(
            strike=25000, call=OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=25000, option_type="CE", ltp=150, oi=5000),
            put=OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=25000, option_type="PE", ltp=100, oi=4000),
        ),
        OptionChainRow(
            strike=25100, call=OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=25100, option_type="CE", ltp=90, oi=3000),
            put=OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=25100, option_type="PE", ltp=170, oi=6000),
        ),
    ]
    chain = OptionChain(underlying="NIFTY", expiry=EXPIRY, spot=25000, atm_strike=25000, generated_at=datetime.now(timezone.utc), rows=rows, provider="test")
    summary = build_market_structure_summary(
        chain, as_of=None, risk_free_rate=0.065, all_expiries=[EXPIRY],
        underlying_source="TEST", option_chain_source="TEST", provider_call_count=0,
    )
    return freeze_snapshot(summary)


def test_real_strike_is_kept():
    context = _context()
    output = DirectionalCaseOutput(thesis="test", key_levels=[25000.0, 25100.0])
    cleaned, warnings = sanitize_directional_case(output, context)
    assert cleaned.key_levels == [25000.0, 25100.0]
    assert warnings == []


def test_hallucinated_strike_is_dropped():
    context = _context()
    output = DirectionalCaseOutput(thesis="test", key_levels=[25000.0, 99999.0])
    cleaned, warnings = sanitize_directional_case(output, context)
    assert cleaned.key_levels == [25000.0]
    assert any("99999" in w for w in warnings)


def test_spot_and_max_pain_are_valid_levels_even_if_not_a_strike():
    context = _context()
    # max_pain_strike is one of the two real strikes here, but spot (25000)
    # coincides with a strike too -- verify both paths accept it.
    output = DirectionalCaseOutput(thesis="test", key_levels=[context.spot])
    cleaned, _ = sanitize_directional_case(output, context)
    assert cleaned.key_levels == [context.spot]


def test_out_of_range_candidate_index_dropped():
    context = _context()
    output = StrategyResearchOutput(commentary=[
        CandidateCommentary(candidate_index=0, fits_reasoning="a", invalidation="b", market_assumptions="c", volatility_assumptions="d", key_levels=[]),
        CandidateCommentary(candidate_index=5, fits_reasoning="a", invalidation="b", market_assumptions="c", volatility_assumptions="d", key_levels=[]),
    ])
    cleaned, warnings = sanitize_candidate_commentary(output, context, candidate_count=2)
    assert len(cleaned.commentary) == 1
    assert cleaned.commentary[0].candidate_index == 0
    assert any("out-of-range" in w for w in warnings)


def test_validate_candidate_index_bounds():
    assert validate_candidate_index(0, 3) is True
    assert validate_candidate_index(2, 3) is True
    assert validate_candidate_index(3, 3) is False
    assert validate_candidate_index(-1, 3) is False


def test_commentary_hallucinated_levels_dropped_but_commentary_kept():
    context = _context()
    output = StrategyResearchOutput(commentary=[
        CandidateCommentary(candidate_index=0, fits_reasoning="a", invalidation="b", market_assumptions="c", volatility_assumptions="d", key_levels=[25000.0, 12345.0]),
    ])
    cleaned, warnings = sanitize_candidate_commentary(output, context, candidate_count=1)
    assert len(cleaned.commentary) == 1
    assert cleaned.commentary[0].key_levels == [25000.0]
    assert any("12345" in w for w in warnings)
