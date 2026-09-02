"""
Market-data API (Stage 19).

    GET  /api/market/session/status     VIEW   -- session/feed state (NO token)
    POST /api/market/session            ADMIN  -- set today's Breeze session token, re-check
    GET  /api/market/indices            VIEW   -- NIFTY / BANKNIFTY / INDIA_VIX / SENSEX
    GET  /api/market/indices/{symbol}   VIEW
    GET  /api/market/nifty/expiries     VIEW
    GET  /api/market/nifty/strikes      VIEW   ?expiry=current|next|YYYY-MM-DD
    GET  /api/market/nifty/option-chain VIEW   ?expiry=current&range=10
    GET  /api/market/candles/{symbol}   VIEW   ?interval=1minute&limit=&from=&to=
    GET  /api/market/health             VIEW

All reads require only VIEW. The session token is write-only: accepted in
the POST body, stored in-process, never returned by any response or log.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.api.deps import Principal, client_ip, enforce_rate_limit, get_db, require_permission
from trading.api.security import audit
from trading.api.security.permissions import Permission
from trading.core.config import load_settings
from trading.database import models
from trading.market_data import option_chain as oc
from trading.market_data.service import get_service
from trading.market_data.session import get_session_manager
from trading.market_data.status import FEED_STATUS, FeedState, SessionState, fingerprint
from trading.market_data.symbols import INDEX_SYMBOLS, normalize_index_symbol, parse_option_symbol

router = APIRouter(
    prefix="/market",
    tags=["market-data"],
    dependencies=[Depends(enforce_rate_limit)],
)

_VIEW = require_permission(Permission.VIEW)
_ADMIN = require_permission(Permission.ADMIN)


class MarketSessionIn(BaseModel):
    # The daily Breeze session token from the supported login flow.
    session_token: str = Field(min_length=1, max_length=4096)
    # Optional: rotate the app key/secret too (normally stable).
    api_key: str | None = Field(default=None, max_length=512)
    secret_key: str | None = Field(default=None, max_length=512)


class MarketSessionStatusOut(BaseModel):
    provider: str
    enabled: bool
    session_state: str
    feed_state: str
    credentials: dict
    last_session_check: datetime | None = None
    last_error: str | None = None


def _derive_feed_state(snap: dict) -> str:
    if not snap.get("enabled"):
        return FeedState.NOT_CONFIGURED.value
    ss = snap.get("session_state")
    if ss == SessionState.NOT_CONFIGURED.value:
        return FeedState.NOT_CONFIGURED.value
    if ss in (SessionState.SESSION_REQUIRED.value, SessionState.UNKNOWN.value):
        return FeedState.SESSION_REQUIRED.value
    if ss == SessionState.ERROR.value:
        return FeedState.ERROR.value
    # session VALID: the scheduler/worker own RUNNING/STALE/etc.
    return snap.get("feed_state") or FeedState.STOPPED.value


def _payload() -> MarketSessionStatusOut:
    snap = FEED_STATUS.snapshot()
    return MarketSessionStatusOut(
        provider=snap["provider"],
        enabled=snap["enabled"],
        session_state=snap["session_state"],
        feed_state=_derive_feed_state(snap),
        credentials=snap["credentials"],
        last_session_check=_parse(snap.get("last_session_check")),
        last_error=snap.get("last_error"),
    )


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


@router.get("/session/status", response_model=MarketSessionStatusOut)
def session_status(principal: Principal = Depends(_VIEW)) -> MarketSessionStatusOut:
    mgr = get_session_manager()
    # On first read of a configured-but-unchecked process, probe once so
    # the dashboard reflects reality without waiting for the scheduler.
    if mgr.last_check is None and load_settings().market_data_enabled:
        mgr.check()
    return _payload()


@router.post("/session", response_model=MarketSessionStatusOut)
def update_session(
    body: MarketSessionIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_ADMIN),
) -> MarketSessionStatusOut:
    mgr = get_session_manager()
    mgr.set_credentials(
        session_token=body.session_token, api_key=body.api_key, secret_key=body.secret_key
    )
    check = mgr.check()
    audit.record(
        db,
        actor=principal.actor,
        actor_label=principal.label,
        action=audit.MARKET_SESSION_UPDATED,
        target="breeze:session",
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={
            "result": check.state.value,
            "session_token_fingerprint": fingerprint(body.session_token),
            "rotated_api_key": bool(body.api_key),
            "rotated_secret_key": bool(body.secret_key),
        },
    )
    return _payload()


# --------------------------------------------------------------------------
# Read endpoints (VIEW). Live data from the in-memory cache; candles from
# PostgreSQL. Never returns credentials.
# --------------------------------------------------------------------------
class IndexQuoteOut(BaseModel):
    symbol: str
    exchange: str
    ltp: float | None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    status: str  # "live" | "stale" | "no_data"
    provider_timestamp: datetime | None = None
    received_at: datetime | None = None


class OptionQuoteOut(BaseModel):
    ltp: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    volume: int | None = None
    oi: int | None = None
    oi_change: int | None = None
    bid: float | None = None
    ask: float | None = None
    iv: float | None = None
    vwap: float | None = None


class OptionChainRowOut(BaseModel):
    strike: float
    call: OptionQuoteOut | None = None
    put: OptionQuoteOut | None = None


class OptionChainOut(BaseModel):
    underlying: str
    spot: float | None
    expiry: date
    atm_strike: float | None
    timestamp: datetime
    strikes: list[OptionChainRowOut]


class CandleOut(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    oi: int | None = None


def _cache():
    return get_service().cache


def _master():
    return get_service().master


def _index_out(symbol: str) -> IndexQuoteOut:
    from trading.market_data.symbols import INDEX_INSTRUMENTS

    inst = INDEX_INSTRUMENTS[symbol]
    entry = _cache().get_latest_quote(symbol)
    if entry is None:
        return IndexQuoteOut(symbol=symbol, exchange=inst.exchange.value, ltp=None, status="no_data")
    q = entry.quote
    return IndexQuoteOut(
        symbol=symbol, exchange=inst.exchange.value, ltp=q.ltp, open=q.open, high=q.high,
        low=q.low, prev_close=q.prev_close, change=q.change, change_percent=q.change_percent,
        status=entry.status(_cache().stale_seconds),
        provider_timestamp=entry.provider_timestamp, received_at=entry.received_at,
    )


@router.get("/indices", response_model=list[IndexQuoteOut])
def get_indices(principal: Principal = Depends(_VIEW)) -> list[IndexQuoteOut]:
    return [_index_out(s) for s in INDEX_SYMBOLS]


@router.get("/indices/{symbol}", response_model=IndexQuoteOut)
def get_index(symbol: str, principal: Principal = Depends(_VIEW)) -> IndexQuoteOut:
    try:
        canonical = normalize_index_symbol(symbol)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown index: {symbol}") from None
    return _index_out(canonical)


@router.get("/nifty/expiries", response_model=list[date])
def get_nifty_expiries(principal: Principal = Depends(_VIEW)) -> list[date]:
    return _master().list_expiries("NIFTY")


@router.get("/nifty/strikes", response_model=list[float])
def get_nifty_strikes(expiry: str = Query("current"), principal: Principal = Depends(_VIEW)) -> list[float]:
    resolved = oc.resolve_expiry(_master(), "NIFTY", expiry)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No expiries known yet (instrument master not loaded)")
    return _master().list_strikes("NIFTY", resolved)


@router.get("/nifty/option-chain", response_model=OptionChainOut)
def get_nifty_option_chain(
    expiry: str = Query("current"),
    range_: int = Query(default=None, ge=1, le=50, alias="range", description="ATM +/- N strikes"),
    principal: Principal = Depends(_VIEW),
) -> OptionChainOut:
    master = _master()
    resolved = oc.resolve_expiry(master, "NIFTY", expiry)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No expiries known yet (instrument master not loaded)")
    n = range_ if range_ is not None else load_settings().nifty_option_strike_range
    spot_entry = _cache().get_latest_quote("NIFTY")
    spot = spot_entry.quote.ltp if spot_entry else None
    chain = oc.build_option_chain(
        underlying="NIFTY", expiry=resolved, spot=spot, cache=_cache(), master=master, strike_range=n,
    )
    return OptionChainOut(
        underlying=chain.underlying, spot=chain.spot, expiry=chain.expiry, atm_strike=chain.atm_strike,
        timestamp=chain.generated_at,
        strikes=[
            OptionChainRowOut(strike=r.strike, call=_opt_out(r.call), put=_opt_out(r.put))
            for r in chain.rows
        ],
    )


def _opt_out(q) -> OptionQuoteOut | None:
    if q is None:
        return None
    return OptionQuoteOut(
        ltp=q.ltp, open=q.open, high=q.high, low=q.low, prev_close=q.prev_close,
        volume=q.volume, oi=q.oi, oi_change=q.oi_change, bid=q.bid, ask=q.ask, iv=q.iv, vwap=q.vwap,
    )


@router.get("/candles/{symbol}", response_model=list[CandleOut])
def get_candles(
    symbol: str,
    interval: str = Query("1minute"),
    limit: int = Query(default=375, ge=1, le=5000),
    db: Session = Depends(get_db),
    principal: Principal = Depends(_VIEW),
) -> list[CandleOut]:
    if "|" in symbol:
        try:
            parse_option_symbol(symbol)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed option symbol") from None
        contract = db.execute(
            select(models.OptionContract).where(models.OptionContract.symbol == symbol)
        ).scalar_one_or_none()
        if contract is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown contract: {symbol}")
        rows = db.execute(
            select(models.OptionCandle)
            .where(models.OptionCandle.contract_id == contract.id)
            .order_by(models.OptionCandle.timestamp.desc())
            .limit(limit)
        ).scalars().all()
    else:
        try:
            canonical = normalize_index_symbol(symbol)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown symbol: {symbol}") from None
        rows = db.execute(
            select(models.MarketCandle)
            .where(models.MarketCandle.symbol == canonical, models.MarketCandle.interval == interval)
            .order_by(models.MarketCandle.timestamp.desc())
            .limit(limit)
        ).scalars().all()
    rows = list(reversed(rows))
    return [
        CandleOut(timestamp=r.timestamp, open=r.open, high=r.high, low=r.low, close=r.close,
                  volume=r.volume, oi=r.oi)
        for r in rows
    ]


class MarketHealthOut(BaseModel):
    status: str
    provider: str
    session: str
    feed: str
    timezone: str
    start_time: str
    stop_time: str
    symbols: dict
    option_chain: dict
    last_error: str | None = None


@router.get("/health", response_model=MarketHealthOut)
def market_health(principal: Principal = Depends(_VIEW)) -> MarketHealthOut:
    s = load_settings()
    snap = FEED_STATUS.snapshot()
    cache = _cache()
    master = _master()

    syms: dict = {}
    for sym in INDEX_SYMBOLS:
        entry = cache.get_latest_quote(sym)
        syms[sym] = {
            "status": "no_data" if entry is None else entry.status(cache.stale_seconds),
            "last_update": entry.received_at.astimezone(timezone.utc).isoformat() if entry else None,
        }

    expiries = master.list_expiries("NIFTY")
    nifty_entry = cache.get_latest_quote("NIFTY")
    atm = None
    if expiries and nifty_entry and nifty_entry.quote.ltp is not None:
        atm = oc.nearest_strike(master.list_strikes("NIFTY", expiries[0]), nifty_entry.quote.ltp)

    feed = snap["feed_state"]
    overall = "healthy" if feed == FeedState.RUNNING.value else (
        "not_configured" if feed == FeedState.NOT_CONFIGURED.value else "degraded"
    )
    return MarketHealthOut(
        status=overall, provider=snap["provider"], session=snap["session_state"], feed=feed,
        timezone=s.market_data_timezone, start_time=s.market_data_start_time,
        stop_time=s.market_data_stop_time, symbols=syms,
        option_chain={
            "status": "live" if expiries else "not_loaded",
            "expiry": expiries[0].isoformat() if expiries else None,
            "atm_strike": atm,
            "contracts_subscribed": snap["option_contracts_subscribed"],
        },
        last_error=snap["last_error"],
    )
