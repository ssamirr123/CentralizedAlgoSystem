"""API-facing schemas for the market domain (Section 31's new endpoints,
and the market/exchange fields added to ResearchRequestIn)."""
from __future__ import annotations

from datetime import date as date_

from pydantic import BaseModel

from trading.ai_research.market.instruments import Exchange, InstrumentType, Market


class InstrumentOut(BaseModel):
    canonical_id: str
    market: Market
    exchange: Exchange
    instrument_type: InstrumentType
    symbol: str
    display_name: str
    currency: str
    timezone: str


class MarketsOut(BaseModel):
    """GET /api/ai-research/markets"""

    markets: list[str]
    default: str = Market.US.value


class InstrumentSearchOut(BaseModel):
    """GET /api/ai-research/instruments"""

    items: list[InstrumentOut]


class TradingDateValidationOut(BaseModel):
    """Section 19: honest non-trading-date feedback -- never a silently
    substituted date."""

    requested: date_
    is_valid: bool
    reason: str | None = None
    previous_trading_day: date_ | None = None
    next_trading_day: date_ | None = None


class CalendarOut(BaseModel):
    """GET /api/ai-research/calendar"""

    market: Market
    timezone: str
    covered_years: list[int]
    validation: TradingDateValidationOut | None = None
