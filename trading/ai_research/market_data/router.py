"""
Market Data API (Phase 8, Section 26/27) -- read-only. Exposes the
HistoricalMarketDataService (DB-first, bounded-backfill) to the frontend
for status/diagnostics. Never exposes Breeze credentials/configuration
(Section 46).

    GET /api/market-data/candles   ?market=&instrument=&interval=&from=&to=[&research_date=]
    GET /api/market-data/status    ?market=&instrument=&interval=&from=&to=
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from trading.ai_research.market.instruments import InvalidSymbolError, Market, resolve_instrument
from trading.ai_research.market_data.schemas import DataSource, DataStatus
from trading.ai_research.market_data.service import get_historical_candles
from trading.api.deps import enforce_rate_limit, require_permission
from trading.api.security.permissions import Permission
from trading.market_data.providers.icici_breeze import BACKTEST_SUPPORTED_INTERVALS

from pydantic import BaseModel

router = APIRouter(prefix="/market-data", tags=["ai-research-market-data"], dependencies=[Depends(enforce_rate_limit)])

_VIEW = require_permission(Permission.VIEW)


class CandleOut(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    oi: int | None = None


class MarketDataOut(BaseModel):
    """Section 19 provenance + Section 30 diagnostics."""

    status: str
    source: str
    provider: str
    canonical_id: str
    symbol: str
    interval: str
    requested_from: date
    requested_to: date
    cutoff_applied: str | None = None
    candle_count: int
    db_hit_count: int
    provider_call_count: int
    missing_days: list[date]
    detail: str | None = None
    candles: list[CandleOut] = []


def _to_out(result, *, include_candles: bool) -> MarketDataOut:
    return MarketDataOut(
        status=result.status, source=result.source, provider=result.provider,
        canonical_id=result.canonical_id, symbol=result.symbol, interval=result.interval,
        requested_from=result.requested_from, requested_to=result.requested_to,
        cutoff_applied=result.cutoff_applied.isoformat() if result.cutoff_applied else None,
        candle_count=len(result.candles), db_hit_count=result.db_hit_count,
        provider_call_count=result.provider_call_count, missing_days=list(result.missing_days),
        detail=result.detail,
        candles=[
            CandleOut(timestamp=c.timestamp.isoformat(), open=c.open, high=c.high, low=c.low,
                      close=c.close, volume=c.volume, oi=c.oi)
            for c in result.candles
        ] if include_candles else [],
    )


def _resolve_or_422(market: Market, instrument: str):
    try:
        return resolve_instrument(market, instrument)
    except InvalidSymbolError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@router.get("/candles", response_model=MarketDataOut)
def get_candles(
    market: Market = Query(...), instrument: str = Query(...),
    interval: str = Query("5minute"), date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"), research_date: date | None = Query(None),
    principal=Depends(_VIEW),
) -> MarketDataOut:
    if interval not in BACKTEST_SUPPORTED_INTERVALS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unsupported interval {interval!r}; supported: {sorted(BACKTEST_SUPPORTED_INTERVALS)}",
        )
    ai_instrument = _resolve_or_422(market, instrument)
    # get_historical_candles() never raises for "no Breeze coverage" --
    # it returns a DataStatus.UNSUPPORTED result (Section 30/31: an
    # honest diagnostic, not a fabricated result or a silent fallback).
    result = get_historical_candles(
        ai_instrument, interval, date_from, date_to, research_date_cutoff=research_date,
    )
    return _to_out(result, include_candles=True)


@router.get("/status", response_model=MarketDataOut)
def get_status(
    market: Market = Query(...), instrument: str = Query(...),
    interval: str = Query("5minute"), date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
    principal=Depends(_VIEW),
) -> MarketDataOut:
    """Section 27: same computation as /candles but without shipping the
    (potentially large) candle payload -- for UI diagnostics."""
    ai_instrument = _resolve_or_422(market, instrument)
    # allow_backfill=False: status is a read-only diagnostic, never
    # itself triggers a provider fetch (Section 34: ingestion and
    # research/diagnostics stay separate).
    result = get_historical_candles(
        ai_instrument, interval, date_from, date_to, allow_backfill=False,
    )
    return _to_out(result, include_candles=False)
