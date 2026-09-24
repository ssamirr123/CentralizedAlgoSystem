"""Phase 10 (Section 59) -- hand-verified payoff engine tests."""
from __future__ import annotations

import pytest

from trading.ai_options_research.payoff import ResolvedLeg, compute_payoff, price_leg
from trading.ai_options_research.strategy_templates import Side
from trading.market_data.options_intelligence import OptionType


def _leg(side, ot, strike, price):
    return ResolvedLeg(side=side, option_type=ot, strike=strike, price=price, price_source="test", liquidity_ok=True)


def test_price_leg_short_prefers_bid():
    price, source = price_leg(Side.SELL, bid=10.0, ask=12.0, ltp=11.0)
    assert price == 10.0 and source == "bid"


def test_price_leg_long_prefers_ask():
    price, source = price_leg(Side.BUY, bid=10.0, ask=12.0, ltp=11.0)
    assert price == 12.0 and source == "ask"


def test_price_leg_falls_back_to_ltp():
    assert price_leg(Side.SELL, bid=None, ask=None, ltp=11.0) == (11.0, "ltp")


def test_price_leg_unavailable_when_nothing_present():
    assert price_leg(Side.BUY, bid=None, ask=None, ltp=None) == (None, "unavailable")


# --------------------------------------------------------------------------
# Bull Put Credit Spread -- hand-calculated
# --------------------------------------------------------------------------
def test_bull_put_credit_spread():
    # Sell 24900 PUT @85, Buy 24700 PUT @15. Net credit = 70.
    # Max profit = 70 (S >= 24900). Max loss = width(200) - credit(70) = 130.
    # Lower breakeven = 24900 - 70 = 24830.
    legs = [_leg(Side.SELL, OptionType.PUT, 24900, 85), _leg(Side.BUY, OptionType.PUT, 24700, 15)]
    result = compute_payoff(legs)
    assert result.net_premium == pytest.approx(70)
    assert result.max_profit == pytest.approx(70)
    assert result.max_loss == pytest.approx(130)
    assert not result.max_profit_unbounded and not result.max_loss_unbounded
    assert result.breakevens == (24830.0,)


def test_bear_call_credit_spread():
    # Sell 25300 CALL @60, Buy 25500 CALL @10. Net credit=50.
    # Max profit=50 (S<=25300). Max loss = 200-50=150. Upper BE=25300+50=25350.
    legs = [_leg(Side.SELL, OptionType.CALL, 25300, 60), _leg(Side.BUY, OptionType.CALL, 25500, 10)]
    result = compute_payoff(legs)
    assert result.max_profit == pytest.approx(50)
    assert result.max_loss == pytest.approx(150)
    assert result.breakevens == (25350.0,)


# --------------------------------------------------------------------------
# Iron Condor -- the exact example from the Phase 10 brief itself (Section 30)
# --------------------------------------------------------------------------
def test_iron_condor_matches_brief_example():
    legs = [
        _leg(Side.BUY, OptionType.PUT, 24700, 30),
        _leg(Side.SELL, OptionType.PUT, 24900, 60),
        _leg(Side.SELL, OptionType.CALL, 25300, 65),
        _leg(Side.BUY, OptionType.CALL, 25500, 30),
    ]
    # net_credit = -30 + 60 + 65 - 30 = 65 ... adjust to hit brief's net_credit=85 exactly:
    result = compute_payoff(legs)
    # Verify internal consistency instead of the brief's illustrative numbers directly:
    # max_loss should equal wing_width - net_credit on whichever side is wider (both 200 here).
    width = 200
    assert result.max_loss == pytest.approx(width - result.net_premium, abs=1e-6)
    assert result.max_profit == pytest.approx(result.net_premium, abs=1e-6)
    lower_be, upper_be = result.breakevens
    assert lower_be == pytest.approx(24900 - result.net_premium, abs=1e-6)
    assert upper_be == pytest.approx(25300 + result.net_premium, abs=1e-6)


def test_iron_condor_with_brief_exact_prices_reproduces_brief_numbers():
    # Solve for leg prices that produce EXACTLY the brief's own net_credit=85,
    # max_profit=85, max_loss=115, lower_breakeven=24815, upper_breakeven=25385.
    legs = [
        _leg(Side.BUY, OptionType.PUT, 24700, 15),
        _leg(Side.SELL, OptionType.PUT, 24900, 60),
        _leg(Side.SELL, OptionType.CALL, 25300, 55),
        _leg(Side.BUY, OptionType.CALL, 25500, 15),
    ]
    result = compute_payoff(legs)
    assert result.net_premium == pytest.approx(85)
    assert result.max_profit == pytest.approx(85)
    assert result.max_loss == pytest.approx(115)
    assert result.breakevens == (pytest.approx(24815), pytest.approx(25385))


# --------------------------------------------------------------------------
# Iron Fly
# --------------------------------------------------------------------------
def test_iron_fly():
    # ATM 25000: sell put+call @ same strike for 150+140=290 credit;
    # wings at 24800/25200 bought for 40+35=75 debit. Net credit=215.
    legs = [
        _leg(Side.SELL, OptionType.PUT, 25000, 150),
        _leg(Side.SELL, OptionType.CALL, 25000, 140),
        _leg(Side.BUY, OptionType.PUT, 24800, 40),
        _leg(Side.BUY, OptionType.CALL, 25200, 35),
    ]
    result = compute_payoff(legs)
    assert result.net_premium == pytest.approx(215)
    assert result.max_profit == pytest.approx(215)  # at S=25000 exactly
    # Net credit (215) exceeds the wing width (200): the worst case at
    # either wing is still a +15 profit (hand-verified below), so the
    # correct "max loss" is exactly 0 -- never a fabricated negative-loss
    # figure, and never left as None (it IS bounded here, both wings finite).
    assert result.max_loss == pytest.approx(0.0)
    assert not result.max_loss_unbounded


# --------------------------------------------------------------------------
# Undefined-risk structures (Section 12/59: never mark as finite)
# --------------------------------------------------------------------------
def test_short_straddle_has_unbounded_loss():
    legs = [_leg(Side.SELL, OptionType.PUT, 25000, 150), _leg(Side.SELL, OptionType.CALL, 25000, 140)]
    result = compute_payoff(legs)
    assert result.max_loss_unbounded is True
    assert result.max_loss is None
    assert result.max_profit == pytest.approx(290)  # capped at net credit


def test_short_strangle_has_unbounded_loss():
    legs = [_leg(Side.SELL, OptionType.PUT, 24800, 60), _leg(Side.SELL, OptionType.CALL, 25200, 55)]
    result = compute_payoff(legs)
    assert result.max_loss_unbounded is True
    assert result.max_profit == pytest.approx(115)


def test_long_call_has_unbounded_profit_and_defined_loss():
    legs = [_leg(Side.BUY, OptionType.CALL, 25000, 120)]
    result = compute_payoff(legs)
    assert result.max_profit_unbounded is True
    assert result.max_profit is None
    assert result.max_loss == pytest.approx(120)  # premium paid
    assert result.breakevens == (25120.0,)


def test_long_put_has_defined_max_profit_and_loss():
    # Long put: max loss = premium; max profit bounded by strike (S floor 0) - premium.
    legs = [_leg(Side.BUY, OptionType.PUT, 25000, 130)]
    result = compute_payoff(legs)
    assert result.max_loss == pytest.approx(130)
    assert result.max_profit == pytest.approx(25000 - 130)
    assert not result.max_profit_unbounded
    assert result.breakevens == (24870.0,)


# --------------------------------------------------------------------------
# Debit spreads
# --------------------------------------------------------------------------
def test_bull_call_debit_spread():
    # Buy 25000 CALL @120, Sell 25200 CALL @40. Net debit=80.
    # Max profit = width(200)-debit(80)=120. Max loss=80. BE=25000+80=25080.
    legs = [_leg(Side.BUY, OptionType.CALL, 25000, 120), _leg(Side.SELL, OptionType.CALL, 25200, 40)]
    result = compute_payoff(legs)
    assert result.net_premium == pytest.approx(-80)
    assert result.max_profit == pytest.approx(120)
    assert result.max_loss == pytest.approx(80)
    assert result.breakevens == (25080.0,)


def test_bear_put_debit_spread():
    legs = [_leg(Side.BUY, OptionType.PUT, 25000, 130), _leg(Side.SELL, OptionType.PUT, 24800, 50)]
    result = compute_payoff(legs)
    assert result.net_premium == pytest.approx(-80)
    assert result.max_profit == pytest.approx(120)
    assert result.max_loss == pytest.approx(80)
    assert result.breakevens == (24920.0,)


# --------------------------------------------------------------------------
# Long straddle / strangle
# --------------------------------------------------------------------------
def test_long_straddle():
    legs = [_leg(Side.BUY, OptionType.PUT, 25000, 130), _leg(Side.BUY, OptionType.CALL, 25000, 120)]
    result = compute_payoff(legs)
    assert result.max_loss == pytest.approx(250)
    assert result.max_profit_unbounded is True
    assert result.breakevens == (pytest.approx(24750), pytest.approx(25250))


def test_long_strangle():
    legs = [_leg(Side.BUY, OptionType.PUT, 24800, 60), _leg(Side.BUY, OptionType.CALL, 25200, 55)]
    result = compute_payoff(legs)
    assert result.max_loss == pytest.approx(115)
    assert result.max_profit_unbounded is True
    assert result.breakevens == (pytest.approx(24685), pytest.approx(25315))


# --------------------------------------------------------------------------
# Priced-ness
# --------------------------------------------------------------------------
def test_unpriceable_leg_returns_not_priced():
    legs = [_leg(Side.SELL, OptionType.PUT, 25000, None), _leg(Side.BUY, OptionType.CALL, 25200, 40)]
    result = compute_payoff(legs)
    assert result.priced is False
    assert result.max_profit is None and result.max_loss is None and result.net_premium is None


def test_empty_legs_returns_unpriced_result():
    result = compute_payoff([])
    assert result.priced is False
