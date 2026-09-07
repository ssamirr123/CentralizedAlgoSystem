"""
Straddle Pulse API -- NIFTY + SENSEX expiry-cycle straddle dashboard.

    GET /market/straddle-pulse/underlyings
    GET /market/straddle-pulse/cycles?underlying=NIFTY|SENSEX
    GET /market/straddle-pulse/cycles/{cycle_id}
    GET /market/straddle-pulse/cycles/{cycle_id}/sessions
    GET /market/straddle-pulse/sessions/{session_id}
    GET /market/straddle-pulse/sessions/{session_id}/chart
    GET /market/straddle-pulse/sessions/{session_id}/oi

Every lookup below a top-level ``underlying`` query param is scoped
through the FK chain (cycle -> session -> underlying match check) so a
NIFTY cycle/session id can never resolve SENSEX rows and vice versa
(spec section 32/39).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.api.deps import Principal, enforce_rate_limit, get_db, require_permission
from trading.api.security.permissions import Permission
from trading.database import models
from trading.market_data.symbols import parse_option_symbol
from trading.market_data.underlying_config import STRADDLE_PULSE_UNDERLYINGS, underlying_config

router = APIRouter(
    prefix="/market/straddle-pulse",
    tags=["straddle-pulse"],
    dependencies=[Depends(enforce_rate_limit)],
)

_VIEW = require_permission(Permission.VIEW)


def _underlying(value: str) -> str:
    key = value.strip().upper()
    if key not in STRADDLE_PULSE_UNDERLYINGS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown underlying: {value}")
    return key


class UnderlyingOut(BaseModel):
    symbol: str
    spot_exchange: str
    option_exchange: str


class CycleOut(BaseModel):
    id: int
    underlying: str
    exchange: str
    expiry_date: date
    cycle_start_date: date
    cycle_end_date: date
    status: str


class SessionOut(BaseModel):
    id: int
    cycle_id: int
    underlying: str
    trading_date: date
    spot_0916: float | None
    atm_strike: float | None
    atm_ce_symbol: str | None
    atm_pe_symbol: str | None
    session_status: str


class CandleOut(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    oi: int | None = None


class SessionChartOut(BaseModel):
    session_id: int
    underlying: str
    trading_date: date
    atm_strike: float | None
    spot: list[CandleOut]
    atm_ce: list[CandleOut]
    atm_pe: list[CandleOut]


class OIPointOut(BaseModel):
    timestamp: datetime
    call_oi_total: int
    put_oi_total: int
    call_oi_change: int
    put_oi_change: int
    pcr: float | None


class SessionOIOut(BaseModel):
    session_id: int
    underlying: str
    trading_date: date
    points: list[OIPointOut]


def _cycle_out(row: models.ExpiryCycle) -> CycleOut:
    return CycleOut(
        id=row.id, underlying=row.underlying, exchange=row.exchange, expiry_date=row.expiry_date,
        cycle_start_date=row.cycle_start_date, cycle_end_date=row.cycle_end_date, status=row.status,
    )


def _session_out(row: models.DailySession) -> SessionOut:
    return SessionOut(
        id=row.id, cycle_id=row.cycle_id, underlying=row.underlying, trading_date=row.trading_date,
        spot_0916=row.spot_0916, atm_strike=row.atm_strike,
        atm_ce_symbol=row.atm_ce_symbol, atm_pe_symbol=row.atm_pe_symbol,
        session_status=row.session_status,
    )


@router.get("/underlyings", response_model=list[UnderlyingOut])
def list_underlyings(principal: Principal = Depends(_VIEW)) -> list[UnderlyingOut]:
    return [
        UnderlyingOut(
            symbol=sym,
            spot_exchange=underlying_config(sym).spot_exchange.value,
            option_exchange=underlying_config(sym).option_exchange.value,
        )
        for sym in STRADDLE_PULSE_UNDERLYINGS
    ]


@router.get("/cycles", response_model=list[CycleOut])
def list_cycles(
    underlying: str = Query(...),
    db: Session = Depends(get_db),
    principal: Principal = Depends(_VIEW),
) -> list[CycleOut]:
    u = _underlying(underlying)
    rows = db.execute(
        select(models.ExpiryCycle)
        .where(models.ExpiryCycle.underlying == u)
        .order_by(models.ExpiryCycle.expiry_date.desc())
    ).scalars().all()
    return [_cycle_out(r) for r in rows]


def _get_cycle(db: Session, cycle_id: int) -> models.ExpiryCycle:
    row = db.get(models.ExpiryCycle, cycle_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown cycle: {cycle_id}")
    return row


@router.get("/cycles/{cycle_id}", response_model=CycleOut)
def get_cycle(cycle_id: int, db: Session = Depends(get_db), principal: Principal = Depends(_VIEW)) -> CycleOut:
    return _cycle_out(_get_cycle(db, cycle_id))


@router.get("/cycles/{cycle_id}/sessions", response_model=list[SessionOut])
def get_cycle_sessions(
    cycle_id: int, db: Session = Depends(get_db), principal: Principal = Depends(_VIEW),
) -> list[SessionOut]:
    cycle = _get_cycle(db, cycle_id)
    rows = db.execute(
        select(models.DailySession)
        .where(models.DailySession.cycle_id == cycle.id)
        .order_by(models.DailySession.trading_date.asc())
    ).scalars().all()
    return [_session_out(r) for r in rows]


def _get_session(db: Session, session_id: int) -> models.DailySession:
    row = db.get(models.DailySession, session_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown session: {session_id}")
    return row


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: int, db: Session = Depends(get_db), principal: Principal = Depends(_VIEW)) -> SessionOut:
    return _session_out(_get_session(db, session_id))


def _day_bounds_utc(trading_date: date) -> tuple[datetime, datetime]:
    """One trading day's [start, end) in UTC, computed from IST midnights --
    generous enough to cover the whole session regardless of DST-free IST's
    fixed +05:30 offset, and keeps one day's candles from bleeding into
    another day's chart (spot is shared across a symbol's whole history;
    the session/date scoping happens here, not on the MarketCandle table)."""
    ist = ZoneInfo("Asia/Kolkata")
    start = datetime.combine(trading_date, datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def _candles_for_symbol(db: Session, symbol: str | None, trading_date: date) -> list[CandleOut]:
    if symbol is None:
        return []
    day_start, day_end = _day_bounds_utc(trading_date)
    if "|" in symbol:
        try:
            parse_option_symbol(symbol)
        except ValueError:
            return []
        contract = db.execute(
            select(models.OptionContract).where(models.OptionContract.symbol == symbol)
        ).scalar_one_or_none()
        if contract is None:
            return []
        rows = db.execute(
            select(models.OptionCandle)
            .where(
                models.OptionCandle.contract_id == contract.id,
                models.OptionCandle.timestamp >= day_start,
                models.OptionCandle.timestamp < day_end,
            )
            .order_by(models.OptionCandle.timestamp.asc())
        ).scalars().all()
    else:
        rows = db.execute(
            select(models.MarketCandle)
            .where(
                models.MarketCandle.symbol == symbol, models.MarketCandle.interval == "1minute",
                models.MarketCandle.timestamp >= day_start, models.MarketCandle.timestamp < day_end,
            )
            .order_by(models.MarketCandle.timestamp.asc())
        ).scalars().all()
    return [
        CandleOut(timestamp=r.timestamp, open=r.open, high=r.high, low=r.low, close=r.close,
                  volume=r.volume, oi=r.oi)
        for r in rows
    ]


@router.get("/sessions/{session_id}/chart", response_model=SessionChartOut)
def get_session_chart(
    session_id: int, db: Session = Depends(get_db), principal: Principal = Depends(_VIEW),
) -> SessionChartOut:
    session = _get_session(db, session_id)
    return SessionChartOut(
        session_id=session.id, underlying=session.underlying, trading_date=session.trading_date,
        atm_strike=session.atm_strike,
        spot=_candles_for_symbol(db, session.underlying, session.trading_date),
        atm_ce=_candles_for_symbol(db, session.atm_ce_symbol, session.trading_date),
        atm_pe=_candles_for_symbol(db, session.atm_pe_symbol, session.trading_date),
    )


@router.get("/sessions/{session_id}/oi", response_model=SessionOIOut)
def get_session_oi(
    session_id: int, db: Session = Depends(get_db), principal: Principal = Depends(_VIEW),
) -> SessionOIOut:
    session = _get_session(db, session_id)
    rows = db.execute(
        select(models.OISnapshot)
        .where(
            models.OISnapshot.cycle_id == session.cycle_id,
            models.OISnapshot.underlying == session.underlying,
            models.OISnapshot.trading_date == session.trading_date,
        )
        .order_by(models.OISnapshot.timestamp.asc())
    ).scalars().all()
    return SessionOIOut(
        session_id=session.id, underlying=session.underlying, trading_date=session.trading_date,
        points=[
            OIPointOut(
                timestamp=r.timestamp, call_oi_total=r.call_oi_total, put_oi_total=r.put_oi_total,
                call_oi_change=r.call_oi_change, put_oi_change=r.put_oi_change, pcr=r.pcr,
            )
            for r in rows
        ],
    )
