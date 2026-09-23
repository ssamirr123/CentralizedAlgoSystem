"""
Market domain API (Section 31) -- follows the existing AI Research route
conventions. Read-only: no route here creates a job, calls TradingAgents,
or touches a broker/order/strategy concept of any kind.

    GET /api/ai-research/markets                          supported markets
    GET /api/ai-research/instruments?market=&query=        Section 26 search
    GET /api/ai-research/calendar?market=&date=            Section 19 date validation

IMPORTANT ROUTING NOTE: mirrors Phase 6's backtests router -- this router
is registered BEFORE ai_research_router in app.py so its literal paths
("/markets", "/instruments", "/calendar") are matched before that
router's GET /ai-research/{research_id} catch-all could shadow them.
"""
from __future__ import annotations

from datetime import date as date_

from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading.ai_research.market.calendar_nse import CalendarCoverageError, validate_research_date
from trading.ai_research.market.instruments import InvalidSymbolError, Market, search_instruments
from trading.ai_research.market.schemas import (
    CalendarOut,
    InstrumentOut,
    InstrumentSearchOut,
    MarketsOut,
    TradingDateValidationOut,
)
from trading.api.deps import enforce_rate_limit, require_permission
from trading.api.security.permissions import Permission

router = APIRouter(prefix="/ai-research", tags=["ai-research-market"], dependencies=[Depends(enforce_rate_limit)])

_VIEW = require_permission(Permission.VIEW)


@router.get("/markets", response_model=MarketsOut)
def get_markets(principal=Depends(_VIEW)) -> MarketsOut:
    return MarketsOut(markets=[m.value for m in Market], default=Market.US.value)


@router.get("/instruments", response_model=InstrumentSearchOut)
def get_instruments(
    market: Market = Query(...), query: str = Query("", max_length=64), principal=Depends(_VIEW),
) -> InstrumentSearchOut:
    try:
        results = search_instruments(market, query)
    except InvalidSymbolError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return InstrumentSearchOut(items=[InstrumentOut(**vars(i)) for i in results])


@router.get("/calendar", response_model=CalendarOut)
def get_calendar(
    market: Market = Query(...), date: date_ | None = Query(None), principal=Depends(_VIEW),
) -> CalendarOut:
    if market != Market.INDIA:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"no trading calendar for market {market.value!r}")
    validation = None
    if date is not None:
        try:
            v = validate_research_date(date)
        except CalendarCoverageError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        validation = TradingDateValidationOut(**vars(v))
    return CalendarOut(market=market, timezone="Asia/Kolkata", covered_years=[2025, 2026], validation=validation)
