"""Section 6/30: strict backtest request validation."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from trading.ai_research.backtest.schemas import BacktestFrequency, BacktestRequestIn


def _body(**overrides):
    body = {
        "symbols": ["AAPL"],
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
    }
    body.update(overrides)
    return body


def test_valid_request_parses():
    req = BacktestRequestIn.model_validate(_body())
    assert req.symbols == ["AAPL"]
    assert req.frequency == BacktestFrequency.WEEKLY


def test_symbols_normalized_uppercase():
    req = BacktestRequestIn.model_validate(_body(symbols=["aapl"]))
    assert req.symbols == ["AAPL"]


def test_duplicate_symbols_rejected():
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(symbols=["AAPL", "aapl"]))


def test_invalid_symbol_pattern_rejected():
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(symbols=["AAPL; DROP TABLE"]))


def test_empty_symbols_rejected():
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(symbols=[]))


def test_end_before_start_rejected():
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(start_date="2026-02-01", end_date="2026-01-01"))


def test_start_date_in_future_rejected():
    future = (date.today() + timedelta(days=5)).isoformat()
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(start_date=future, end_date=future))


def test_end_date_in_future_rejected():
    future = (date.today() + timedelta(days=5)).isoformat()
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(end_date=future))


def test_unknown_frequency_rejected():
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(frequency="hourly"))


def test_unsupported_provider_rejected():
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(llm_provider="not-a-real-provider"))


def test_unsupported_data_vendor_rejected():
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(data_vendors={"core_stock_apis": "not-a-vendor"}))


def test_extra_field_rejected():
    """Execution-shaped field rejection, same discipline as ResearchRequestIn."""
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(broker="zerodha"))


def test_holding_days_bounds():
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(holding_days=0))
    with pytest.raises(ValidationError):
        BacktestRequestIn.model_validate(_body(holding_days=61))
    req = BacktestRequestIn.model_validate(_body(holding_days=10))
    assert req.holding_days == 10
