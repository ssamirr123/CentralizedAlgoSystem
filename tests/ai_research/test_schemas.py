"""
Schema validation tests -- Section 6/18: execution-shaped fields must be
REJECTED (422/ValidationError), not silently ignored. Also covers basic
input validation (Section 5) and the research_depth -> debate-rounds
mapping (Section 8).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from trading.ai_research.schemas import (
    PortfolioContextIn,
    ResearchDepth,
    ResearchRequestIn,
)

_TODAY = date.today()
_YESTERDAY = _TODAY - timedelta(days=1)
_TOMORROW = _TODAY + timedelta(days=1)


def _base(**overrides) -> dict:
    body = {"symbol": "AAPL", "research_date": _YESTERDAY.isoformat()}
    body.update(overrides)
    return body


def test_valid_request_parses():
    req = ResearchRequestIn.model_validate(_base())
    assert req.symbol == "AAPL"
    assert req.research_depth == ResearchDepth.STANDARD


@pytest.mark.parametrize("field,value", [
    ("broker", "angelone"),
    ("broker_id", "ANGEL_MAIN"),
    ("account", "ACC1"),
    ("account_id", "ACC1"),
    ("quantity", 100),
    ("order_type", "MARKET"),
    ("price", 150.0),
    ("side", "BUY"),
    ("strategy_id", "CombinedVwapNifty"),
    ("live", True),
    ("paper", False),
    ("trading_mode", "live"),
])
def test_execution_fields_are_rejected_not_ignored(field, value):
    """Section 6/18: exactly the fields the brief calls out must be
    REJECTED, proving extra="forbid" actually works end to end, not just
    in theory."""
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(**{field: value}))


def test_future_date_rejected():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(research_date=_TOMORROW.isoformat()))


def test_unsupported_symbol_characters_rejected():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(symbol="AAPL; DROP TABLE"))


def test_unsupported_llm_provider_rejected():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(llm_provider="not-a-real-provider"))


def test_unsupported_analyst_rejected():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(selected_analysts=["not-a-real-analyst"]))


@pytest.mark.parametrize("depth,expected_debate,expected_risk", [
    ("quick", 1, 1),
    ("standard", 1, 1),
    ("deep", 2, 2),
])
def test_research_depth_maps_to_real_debate_round_keys(depth, expected_debate, expected_risk):
    """Section 8: depth is OUR convenience layer over the real upstream
    max_debate_rounds/max_risk_discuss_rounds config keys -- not invented
    semantics."""
    req = ResearchRequestIn.model_validate(_base(research_depth=depth))
    assert req.resolved_debate_rounds() == expected_debate
    assert req.resolved_risk_debate_rounds() == expected_risk


def test_explicit_debate_rounds_overrides_depth_default():
    req = ResearchRequestIn.model_validate(_base(research_depth="quick", debate_rounds=3))
    assert req.resolved_debate_rounds() == 3


def test_portfolio_context_mirrors_upstream_shape():
    portfolio = PortfolioContextIn.model_validate({
        "cash": 25000, "currency": "USD",
        "positions": [{"ticker": "NVDA", "quantity": 120, "average_price": 150}],
    })
    req = ResearchRequestIn.model_validate(_base(portfolio=portfolio.model_dump()))
    assert req.portfolio.cash == 25000
    assert req.portfolio.positions[0].ticker == "NVDA"


def test_portfolio_context_rejects_execution_fields():
    with pytest.raises(ValidationError):
        PortfolioContextIn.model_validate({"cash": 100, "order_type": "MARKET"})


def test_debate_rounds_bounded():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(debate_rounds=100))


def test_temperature_bounded():
    with pytest.raises(ValidationError):
        ResearchRequestIn.model_validate(_base(temperature=10.0))
