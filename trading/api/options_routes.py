"""
Phase 9 -- Options Intelligence API.

    GET /api/options/expiries      VIEW  ?underlying=NIFTY
    GET /api/options/chain         VIEW  ?underlying=NIFTY&expiry=current&strike_window=5&as_of=...
    GET /api/options/intelligence  VIEW  ?underlying=NIFTY&expiry=current&strike_window=5&as_of=...

Reads only -- no order/strategy endpoints exist here (Section 59). All
three routes go through ``options_service``/``options_intelligence``,
never directly to Breeze from this layer's caller (React/TradingAgents).
"""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from trading.api.deps import Principal, enforce_rate_limit, get_db, require_permission
from trading.api.security.permissions import Permission
from trading.core.config import load_settings
from trading.market_data import option_chain as oc
from trading.market_data.options_intelligence import (
    DataQuality,
    IVSource,
    MarketStructureSummary,
    Moneyness,
    build_market_structure_summary,
)
from trading.market_data.options_service import (
    ChainResult,
    OptionsDataError,
    get_historical_chain,
    get_live_chain_from_cache,
    get_live_chain_via_provider,
)
from trading.market_data.service import get_service
from trading.market_data.underlying_config import UNDERLYING_CONFIGS

logger = logging.getLogger("trading.api.options_routes")


def _require_enabled() -> None:
    """Phase 11 (Section 4): OPTIONS_INTELLIGENCE_ENABLED, independent of
    every other AI/market-data flag. 503 (Section 38: "temporarily
    unavailable"), not a fabricated empty result."""
    if not load_settings().options_intelligence_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Options Intelligence is disabled")


router = APIRouter(
    prefix="/options", tags=["options-intelligence"],
    dependencies=[Depends(enforce_rate_limit), Depends(_require_enabled)],
)

_VIEW = require_permission(Permission.VIEW)

# Section 51: short-lived, current-snapshot-only cache. Keyed on
# (underlying, expiry ISO, strike_window); never used for an as_of request.
_snapshot_cache: dict[tuple[str, str, int], tuple[float, ChainResult]] = {}


def _validate_underlying(underlying: str) -> str:
    under = underlying.strip().upper()
    if under not in UNDERLYING_CONFIGS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported underlying {underlying!r}. Supported: {', '.join(sorted(UNDERLYING_CONFIGS))}",
        )
    return under


def _resolve_expiry_or_404(underlying: str, expiry: str) -> date:
    master = get_service().master
    resolved = oc.resolve_expiry(master, underlying, expiry)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No expiries known yet for {underlying}")
    return resolved


def _resolve_chain(underlying: str, expiry: date, strike_window: int, as_of: datetime | None, db: Session) -> ChainResult:
    if as_of is not None:
        try:
            return get_historical_chain(db, underlying, expiry, as_of, strike_window=strike_window)
        except OptionsDataError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    cache_key = (underlying, expiry.isoformat(), strike_window)
    settings = load_settings()
    cached = _snapshot_cache.get(cache_key)
    now = _time.monotonic()
    if cached is not None and (now - cached[0]) < settings.options_snapshot_cache_seconds:
        return cached[1]

    svc = get_service()
    live_from_cache = get_live_chain_from_cache(
        underlying, expiry, strike_window=strike_window, cache=svc.cache, master=svc.master,
    )
    if any(r.call is not None or r.put is not None for r in live_from_cache.chain.rows):
        _snapshot_cache[cache_key] = (now, live_from_cache)
        return live_from_cache

    try:
        result = get_live_chain_via_provider(underlying, expiry, strike_window=strike_window, settings=settings)
    except Exception as exc:  # noqa: BLE001 -- surfaced as a structured 502, never a raw traceback
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Provider fetch failed: {type(exc).__name__}: {exc}") from None
    _snapshot_cache[cache_key] = (now, result)
    return result


# --------------------------------------------------------------------------
# Response schemas
# --------------------------------------------------------------------------
class ExpiryOut(BaseModel):
    expiry: date
    days_to_expiry_calendar: int
    days_to_expiry_trading: int | None
    is_nearest: bool
    is_monthly: bool


class OptionQuoteOut(BaseModel):
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    change_in_oi: int | None = None
    iv: float | None = None
    iv_source: str | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    moneyness: str | None = None


class ChainRowOut(BaseModel):
    strike: float
    call: OptionQuoteOut | None
    put: OptionQuoteOut | None


class ChainOut(BaseModel):
    underlying: str
    expiry: date
    spot: float | None
    atm_strike: float | None
    timestamp: datetime
    source: str
    provider_call_count: int
    rows: list[ChainRowOut]


class IntelligenceOut(BaseModel):
    underlying: str
    spot: float | None
    expiry: date | None
    atm_strike: float | None
    expiry_info: ExpiryOut | None
    oi_pcr: float | None
    volume_pcr: float | None
    total_call_oi: int | None
    total_put_oi: int | None
    call_oi_concentration: float | None
    put_oi_concentration: float | None
    oi_support: list[float]
    oi_resistance: list[float]
    max_pain_strike: float | None
    max_pain_diagnostics: list[list[float]]
    atm_iv: float | None
    atm_iv_source: str
    expected_move: float | None
    expected_move_method: str
    iv_rank: float | None
    iv_percentile: float | None
    iv_rank_status: str
    iv_rank_reason: str | None
    quality: str
    quality_reasons: list[str]
    provenance: dict
    chain: ChainOut
    as_of: datetime | None


def _quote_out(row_iv: float | None, row_iv_source: str | None, greeks, quote, moneyness) -> OptionQuoteOut | None:
    if quote is None and row_iv is None:
        return None
    return OptionQuoteOut(
        ltp=quote.ltp if quote else None, bid=quote.bid if quote else None, ask=quote.ask if quote else None,
        volume=quote.volume if quote else None, open_interest=quote.oi if quote else None,
        change_in_oi=quote.oi_change if quote else None,
        iv=row_iv, iv_source=row_iv_source,
        delta=greeks.delta if greeks else None, gamma=greeks.gamma if greeks else None,
        theta=greeks.theta_per_day if greeks else None, vega=greeks.vega_per_1pct if greeks else None,
        moneyness=moneyness.value if moneyness else None,
    )


def _chain_out(chain_result: ChainResult) -> ChainOut:
    chain = chain_result.chain
    return ChainOut(
        underlying=chain.underlying, expiry=chain.expiry, spot=chain.spot, atm_strike=chain.atm_strike,
        timestamp=chain.generated_at, source=chain_result.source, provider_call_count=chain_result.provider_call_count,
        rows=[
            ChainRowOut(
                strike=r.strike,
                call=_quote_out(None, None, None, r.call, None) if r.call else None,
                put=_quote_out(None, None, None, r.put, None) if r.put else None,
            )
            for r in chain.rows
        ],
    )


def _summary_out(summary: MarketStructureSummary, chain_result: ChainResult) -> IntelligenceOut:
    by_key = {(r.strike, r.option_type): r for r in summary.rows}
    chain = chain_result.chain
    rows_out = []
    for row in chain.rows:
        from trading.market_data.options_intelligence import OptionType

        call_row = by_key.get((row.strike, OptionType.CALL))
        put_row = by_key.get((row.strike, OptionType.PUT))
        rows_out.append(ChainRowOut(
            strike=row.strike,
            call=_quote_out(call_row.iv if call_row else None, call_row.iv_source.value if call_row else None,
                            call_row.greeks if call_row else None, row.call, call_row.moneyness if call_row else None),
            put=_quote_out(put_row.iv if put_row else None, put_row.iv_source.value if put_row else None,
                           put_row.greeks if put_row else None, row.put, put_row.moneyness if put_row else None),
        ))
    chain_out = ChainOut(
        underlying=chain.underlying, expiry=chain.expiry, spot=chain.spot, atm_strike=chain.atm_strike,
        timestamp=chain.generated_at, source=chain_result.source, provider_call_count=chain_result.provider_call_count,
        rows=rows_out,
    )
    ei = summary.expiry_info
    return IntelligenceOut(
        underlying=summary.underlying, spot=summary.spot, expiry=summary.expiry, atm_strike=summary.atm_strike,
        expiry_info=ExpiryOut(
            expiry=ei.expiry, days_to_expiry_calendar=ei.days_to_expiry_calendar,
            days_to_expiry_trading=ei.days_to_expiry_trading, is_nearest=ei.is_nearest, is_monthly=ei.is_monthly,
        ) if ei else None,
        oi_pcr=summary.pcr.oi_pcr, volume_pcr=summary.pcr.volume_pcr,
        total_call_oi=summary.oi.total_call_oi if summary.oi else None,
        total_put_oi=summary.oi.total_put_oi if summary.oi else None,
        call_oi_concentration=summary.oi.call_oi_concentration if summary.oi else None,
        put_oi_concentration=summary.oi.put_oi_concentration if summary.oi else None,
        oi_support=list(summary.support_resistance.oi_support),
        oi_resistance=list(summary.support_resistance.oi_resistance),
        max_pain_strike=summary.max_pain.max_pain_strike,
        max_pain_diagnostics=[[s, p] for s, p in summary.max_pain.payouts],
        atm_iv=summary.atm_iv, atm_iv_source=summary.atm_iv_source.value,
        expected_move=summary.expected_move.expected_move, expected_move_method=summary.expected_move.method,
        iv_rank=summary.iv_rank.iv_rank, iv_percentile=summary.iv_rank.iv_percentile,
        iv_rank_status=summary.iv_rank.status, iv_rank_reason=summary.iv_rank.reason,
        quality=summary.quality.status.value, quality_reasons=list(summary.quality.reasons),
        provenance={
            "underlying_source": summary.provenance.underlying_source,
            "option_chain_source": summary.provenance.option_chain_source,
            "spot_timestamp": summary.provenance.spot_timestamp.isoformat() if summary.provenance.spot_timestamp else None,
            "chain_timestamp": summary.provenance.chain_timestamp.isoformat() if summary.provenance.chain_timestamp else None,
            "calculation_version": summary.provenance.calculation_version,
            "risk_free_rate": summary.provenance.risk_free_rate,
            "provider_call_count": summary.provenance.provider_call_count,
        },
        chain=chain_out, as_of=summary.as_of,
    )


@router.get("/expiries", response_model=list[ExpiryOut])
def get_expiries(
    underlying: str = Query("NIFTY"), principal: Principal = Depends(_VIEW),
) -> list[ExpiryOut]:
    under = _validate_underlying(underlying)
    master = get_service().master
    expiries = master.list_expiries(under)
    if not expiries:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No expiries known yet for {under}")
    now = datetime.now(timezone.utc)
    from trading.market_data.options_intelligence import build_expiry_info

    return [ExpiryOut(**build_expiry_info(e, expiries, as_of=now).__dict__) for e in expiries]


@router.get("/chain", response_model=ChainOut)
def get_chain(
    underlying: str = Query("NIFTY"),
    expiry: str = Query("current"),
    strike_window: int = Query(default=None, ge=1, le=50),
    as_of: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(_VIEW),
) -> ChainOut:
    under = _validate_underlying(underlying)
    resolved_expiry = _resolve_expiry_or_404(under, expiry)
    n = strike_window if strike_window is not None else load_settings().options_chain_strike_window
    result = _resolve_chain(under, resolved_expiry, n, as_of, db)
    return _chain_out(result)


@router.get("/intelligence", response_model=IntelligenceOut)
def get_intelligence(
    underlying: str = Query("NIFTY"),
    expiry: str = Query("current"),
    strike_window: int = Query(default=None, ge=1, le=50),
    as_of: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(_VIEW),
) -> IntelligenceOut:
    under = _validate_underlying(underlying)
    resolved_expiry = _resolve_expiry_or_404(under, expiry)
    n = strike_window if strike_window is not None else load_settings().options_chain_strike_window
    result = _resolve_chain(under, resolved_expiry, n, as_of, db)

    settings = load_settings()
    all_expiries = get_service().master.list_expiries(under)
    summary = build_market_structure_summary(
        result.chain, as_of=as_of, risk_free_rate=settings.options_risk_free_rate,
        all_expiries=all_expiries or [resolved_expiry],
        underlying_source=result.source, option_chain_source=result.source,
        provider_call_count=result.provider_call_count,
    )
    return _summary_out(summary, result)
