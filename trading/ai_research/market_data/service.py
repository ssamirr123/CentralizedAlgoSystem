"""
HistoricalMarketDataService -- the ONLY thing AI Research/AI Backtesting
talk to for Indian market-price data (Section 6: "AI Research should
depend on MarketDataService, not BreezeProvider directly").

Reuses, never duplicates:
  * trading.market_data.providers.icici_breeze.ICICIBreezeProvider --
    the real Breeze client, session handling, normalization.
  * trading.database.models.MarketCandle -- the EXISTING candles table
    (Section 11: "do NOT create a second market-data database"). Equity
    symbols and non-1-minute intervals are written into this SAME table;
    its (symbol, exchange, interval, timestamp) unique key already
    supports them structurally, no schema change needed.
  * trading.market_data.aggregator.persist_index_candles -- the EXISTING
    idempotent insert (checks existing keys first, falls back to
    per-row insert on an IntegrityError race) -- Section 12 "duplicate
    prevention" is satisfied by reuse, not reimplementation.
  * trading.ai_research.market.calendar_nse -- Phase 7's verified NSE
    trading calendar, for missing-trading-day detection (Section 14) and
    point-in-time cutoff (Section 21/22).

Flow (Section 13):

    request (instrument, interval, start, end, research_date_cutoff)
             |
      resolve canonical -> Stage-19 Instrument (to_breeze_symbol)
             |
      clamp end to cutoff (Section 21/22 -- server-side, never trusts
      the prompt)
             |
      DB read (market_candles)
             |
      missing NSE trading days in range?
        NO -> return DB rows, source=TIMESCALEDB
        YES -> bounded backfill (Section 15/16)
                 |
               fetch (Breeze, bounded retries -- Section 17)
                 |
               validate (Section 18)
                 |
               persist (idempotent -- Section 12)
                 |
               re-read DB, return, source=ICICI_BREEZE or MIXED
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.ai_research.market.calendar_nse import (
    CalendarCoverageError,
    market_close,
    trading_days_in_range,
)
from trading.ai_research.market.instruments import Instrument as AIInstrument
from trading.ai_research.market.instruments import InstrumentType as AIInstrumentType
from trading.ai_research.market.instruments import Market
from trading.ai_research.market.symbols import UnmappedProviderSymbolError, to_breeze_symbol
from trading.ai_research.market_data.schemas import DataSource, DataStatus, HistoricalDataResult
from trading.core.config import load_settings
from trading.database import models
from trading.database.connection import SessionLocal
from trading.market_data.aggregator import persist_index_candles
from trading.market_data.providers import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderDataError,
    ProviderRateLimitError,
    create_market_data_provider,
)
from trading.market_data.schemas import Candle
from trading.market_data.symbols import equity_instrument, index_instrument

logger = logging.getLogger("trading.ai_research.market_data")

# Bounded retry for a single transient connection failure per provider
# call (Section 17: "bounded retries... no unlimited loop"). Rate limits
# are never retried within a request (see _fetch_range).
_CONNECTION_RETRY_ATTEMPTS = 2
_CONNECTION_RETRY_DELAY_SECONDS = 1.0


class MarketDataUnavailableError(RuntimeError):
    """Raised when the request cannot be resolved to a Breeze-backed
    instrument at all (e.g. market=US -- Breeze has no US coverage)."""


def _resolve_stage19_instrument(ai_instrument: AIInstrument):
    """AI Research canonical Instrument -> Stage-19 provider-agnostic
    Instrument (trading.market_data.symbols.Instrument). Raises
    MarketDataUnavailableError for anything Breeze cannot serve."""
    if ai_instrument.market != Market.INDIA:
        raise MarketDataUnavailableError(
            f"no Breeze market-data coverage for market={ai_instrument.market.value!r} "
            "(this system's Breeze integration is India-only)"
        )
    try:
        stage19_symbol = to_breeze_symbol(ai_instrument)
    except UnmappedProviderSymbolError as exc:
        raise MarketDataUnavailableError(str(exc)) from exc

    if ai_instrument.instrument_type == AIInstrumentType.INDEX:
        return index_instrument(stage19_symbol)
    return equity_instrument(stage19_symbol)


def _validate_candles(raw: list[Candle]) -> list[Candle]:
    """Section 18: reject malformed candles before persistence -- never
    silently persist something broken."""
    seen_ts: set[datetime] = set()
    out: list[Candle] = []
    for c in raw:
        if c.timestamp is None:
            continue
        ts = c.timestamp if c.timestamp.tzinfo else c.timestamp.replace(tzinfo=timezone.utc)
        if None in (c.open, c.high, c.low, c.close):
            continue
        if c.high < c.low:
            continue
        if not (c.low <= c.open <= c.high and c.low <= c.close <= c.high):
            continue
        if ts in seen_ts:
            continue  # duplicate timestamp within this batch
        seen_ts.add(ts)
        out.append(Candle(
            symbol=c.symbol, interval=c.interval, timestamp=ts,
            open=c.open, high=c.high, low=c.low, close=c.close,
            volume=c.volume, oi=c.oi,
        ))
    out.sort(key=lambda c: c.timestamp)
    return out


def _contiguous_runs(missing_days: list[date]) -> list[tuple[date, date]]:
    """Group consecutive (calendar-adjacent) missing days into runs, so
    one Breeze call can cover a whole run instead of one call per day
    (Section 14: 'fetch only what is required', Section 15: bounded call
    budget)."""
    if not missing_days:
        return []
    days = sorted(missing_days)
    runs: list[tuple[date, date]] = []
    run_start = run_end = days[0]
    for d in days[1:]:
        if (d - run_end).days <= 3:  # tolerate weekend/holiday gaps within one run
            run_end = d
        else:
            runs.append((run_start, run_end))
            run_start = run_end = d
    runs.append((run_start, run_end))
    return runs


def build_market_data_context(
    ai_instrument: AIInstrument, research_date: date, *, lookback_days: int = 10,
) -> tuple[str | None, dict]:
    """Section 23/29: a compact text block for injection into
    TradingAgents' `instrument_context` (see trading_agents_adapter.py's
    _run_graph_with_progress), plus a small provenance dict for
    persistence/display (Section 19/29/37 -- metadata ABOUT the research
    run, never the candles themselves, so this never mixes the AI
    Research DB with the market-data DB's own row data).

    Returns (context_text_or_None, provenance_dict). context is None when
    there is genuinely nothing to inject (UNSUPPORTED/no candles) -- never
    fabricated placeholder text."""
    from datetime import timedelta

    start = research_date - timedelta(days=lookback_days)
    result = get_historical_candles(
        ai_instrument, "5minute", start, research_date, research_date_cutoff=research_date,
    )
    provenance = {
        "status": result.status, "source": result.source, "provider": result.provider,
        "symbol": result.symbol, "interval": result.interval,
        "from": result.requested_from.isoformat(), "to": result.requested_to.isoformat(),
        "cutoff": result.cutoff_applied.isoformat() if result.cutoff_applied else None,
        "candle_count": len(result.candles),
    }
    if not result.candles:
        return None, provenance

    last = result.candles[-5:]
    lines = [
        f"## Market Data ({provenance['provider']} via {provenance['source']})",
        f"Instrument: {ai_instrument.display_name} ({result.symbol}) | Interval: {result.interval}",
        f"Coverage: {provenance['from']} to {provenance['to']} | Cutoff: {provenance['cutoff']}",
        f"Last {len(last)} candles (timestamp, O, H, L, C, V):",
    ]
    for c in last:
        lines.append(f"  {c.timestamp.isoformat()}  {c.open:.2f}  {c.high:.2f}  {c.low:.2f}  {c.close:.2f}  {c.volume or 0}")
    return "\n".join(lines), provenance


def get_historical_candles(
    ai_instrument: AIInstrument,
    interval: str,
    start: date,
    end: date,
    *,
    research_date_cutoff: date | None = None,
    allow_backfill: bool = True,
    db: Session | None = None,
    provider_factory=None,
) -> HistoricalDataResult:
    """The one entry point AI Research / AI Backtesting call. Never
    raises for an ordinary "no data yet" case -- returns a result with
    status=MISSING/PARTIAL/PROVIDER_ERROR/RATE_LIMITED instead, so a
    caller can decide what to do (Section 30)."""
    # Resolved here (not as a default argument value) so a test's
    # monkeypatch of the module-level create_market_data_provider name
    # actually takes effect -- a default value would bind the function
    # reference once at import time and never see the patch.
    if provider_factory is None:
        provider_factory = create_market_data_provider
    settings = load_settings()

    try:
        stage19_instrument = _resolve_stage19_instrument(ai_instrument)
    except MarketDataUnavailableError as exc:
        return HistoricalDataResult(
            status=DataStatus.UNSUPPORTED, source=DataSource.NONE, provider=settings.market_data_provider,
            canonical_id=ai_instrument.canonical_id, symbol=ai_instrument.symbol, interval=interval,
            requested_from=start, requested_to=end, cutoff_applied=None, detail=str(exc),
        )

    symbol = stage19_instrument.internal_symbol
    exchange = stage19_instrument.exchange.value

    # Section 21/22: server-side cutoff, never relies on the LLM prompt.
    cutoff_dt: datetime | None = None
    if research_date_cutoff is not None:
        try:
            cutoff_dt = market_close(research_date_cutoff)
        except CalendarCoverageError as exc:
            return HistoricalDataResult(
                status=DataStatus.MISSING, source=DataSource.NONE, provider=settings.market_data_provider,
                canonical_id=ai_instrument.canonical_id, symbol=symbol, interval=interval,
                requested_from=start, requested_to=end, cutoff_applied=None, detail=str(exc),
            )
        if cutoff_dt.date() < end:
            end = cutoff_dt.date()
        if start > end:
            return HistoricalDataResult(
                status=DataStatus.MISSING, source=DataSource.NONE, provider=settings.market_data_provider,
                canonical_id=ai_instrument.canonical_id, symbol=symbol, interval=interval,
                requested_from=start, requested_to=end, cutoff_applied=cutoff_dt,
                detail="requested range is entirely after the research-date cutoff",
            )

    owns_session = db is None
    db = db or SessionLocal()
    try:
        result = _run(
            db, ai_instrument, stage19_instrument, symbol, exchange, interval, start, end,
            cutoff_dt, allow_backfill, settings, provider_factory,
        )
    finally:
        if owns_session:
            db.close()
    return result


def _query_db(db: Session, symbol: str, exchange: str, interval: str, start: date, end: date, cutoff_dt: datetime | None) -> list[models.MarketCandle]:
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc)
    stmt = (
        select(models.MarketCandle)
        .where(
            models.MarketCandle.symbol == symbol, models.MarketCandle.exchange == exchange,
            models.MarketCandle.interval == interval,
            models.MarketCandle.timestamp >= start_dt, models.MarketCandle.timestamp <= end_dt,
        )
        .order_by(models.MarketCandle.timestamp)
    )
    rows = list(db.execute(stmt).scalars().all())
    if cutoff_dt is not None:
        rows = [r for r in rows if _aware(r.timestamp) <= cutoff_dt]
    return rows


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _missing_trading_days(rows: list[models.MarketCandle], start: date, end: date) -> list[date]:
    have_days = {_aware(r.timestamp).astimezone(timezone(timedelta(hours=5, minutes=30))).date() for r in rows}
    try:
        expected = trading_days_in_range(start, end)
    except CalendarCoverageError:
        return []
    return [d for d in expected if d not in have_days]


def _fetch_range(
    stage19_instrument, interval: str, run_start: date, run_end: date, settings, provider_factory,
) -> tuple[list[Candle], str | None]:
    """One bounded Breeze call for one contiguous missing-day run.
    Returns (candles, error_status_or_None). Rate limits are never
    retried within a request (Section 17: 'no retry storm'); a transient
    connection failure gets a small bounded retry."""
    provider = provider_factory(
        settings.market_data_provider,
        api_key=settings.breeze_api_key, api_secret=settings.breeze_secret_key,
        session_token=settings.breeze_session_token,
    )
    start_dt = datetime.combine(run_start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(run_end, datetime.max.time(), tzinfo=timezone.utc)

    attempt = 0
    while True:
        attempt += 1
        try:
            provider.connect()
            try:
                candles = provider.get_historical_candles(stage19_instrument, interval, start_dt, end_dt)
            finally:
                provider.disconnect()
            return candles, None
        except ProviderAuthError as exc:
            logger.warning("market_data.provider_auth_error symbol=%s err=%s", stage19_instrument.internal_symbol, type(exc).__name__)
            return [], DataStatus.PROVIDER_ERROR
        except ProviderRateLimitError as exc:
            logger.warning("market_data.rate_limited symbol=%s err=%s", stage19_instrument.internal_symbol, type(exc).__name__)
            return [], DataStatus.RATE_LIMITED
        except (ProviderConnectionError, ProviderDataError) as exc:
            if attempt <= _CONNECTION_RETRY_ATTEMPTS and isinstance(exc, ProviderConnectionError):
                logger.info(
                    "market_data.provider_retry symbol=%s attempt=%d err=%s",
                    stage19_instrument.internal_symbol, attempt, type(exc).__name__,
                )
                time.sleep(_CONNECTION_RETRY_DELAY_SECONDS)
                continue
            logger.warning("market_data.provider_error symbol=%s err=%s", stage19_instrument.internal_symbol, type(exc).__name__)
            return [], DataStatus.PROVIDER_ERROR


def _run(
    db, ai_instrument, stage19_instrument, symbol, exchange, interval, start, end,
    cutoff_dt, allow_backfill, settings, provider_factory,
) -> HistoricalDataResult:
    rows = _query_db(db, symbol, exchange, interval, start, end, cutoff_dt)
    db_hit_count = len(rows)
    missing_days = _missing_trading_days(rows, start, end)
    # Cutoff never leaves a "missing" day that's actually beyond the
    # permitted window from re-triggering a fetch.
    if cutoff_dt is not None:
        missing_days = [d for d in missing_days if d <= cutoff_dt.date()]

    provider_call_count = 0
    candles_fetched = 0
    candles_stored = 0
    last_error_status: str | None = None

    if missing_days and allow_backfill:
        if len(missing_days) > settings.market_data_max_backfill_days:
            logger.info(
                "market_data.backfill_bounded_out symbol=%s missing_days=%d limit=%d",
                symbol, len(missing_days), settings.market_data_max_backfill_days,
            )
        else:
            runs = _contiguous_runs(missing_days)
            for run_start, run_end in runs:
                if provider_call_count >= settings.market_data_max_provider_calls:
                    logger.info("market_data.provider_call_budget_exhausted symbol=%s limit=%d", symbol, settings.market_data_max_provider_calls)
                    break
                t0 = time.monotonic()
                raw, err = _fetch_range(stage19_instrument, interval, run_start, run_end, settings, provider_factory)
                provider_call_count += 1
                duration = time.monotonic() - t0
                if err is not None:
                    last_error_status = err
                    logger.warning(
                        "market_data.fetch_failed symbol=%s run=%s..%s status=%s duration=%.2fs",
                        symbol, run_start, run_end, err, duration,
                    )
                    if err == DataStatus.RATE_LIMITED:
                        break  # never keep calling after a rate limit (Section 17)
                    continue
                valid = _validate_candles(raw)
                candles_fetched += len(valid)
                if cutoff_dt is not None:
                    valid = [c for c in valid if c.timestamp <= cutoff_dt]
                stored = persist_index_candles(db, [(symbol, exchange, c) for c in valid])
                candles_stored += stored
                logger.info(
                    "market_data.fetch_ok symbol=%s run=%s..%s fetched=%d stored=%d duration=%.2fs",
                    symbol, run_start, run_end, len(valid), stored, duration,
                )
            rows = _query_db(db, symbol, exchange, interval, start, end, cutoff_dt)
            missing_days = _missing_trading_days(rows, start, end)
            if cutoff_dt is not None:
                missing_days = [d for d in missing_days if d <= cutoff_dt.date()]

    candles = tuple(
        Candle(symbol=symbol, interval=interval, timestamp=_aware(r.timestamp), open=r.open, high=r.high,
               low=r.low, close=r.close, volume=r.volume, oi=r.oi)
        for r in rows
    )

    if provider_call_count == 0:
        source = DataSource.DB if candles else DataSource.NONE
    elif db_hit_count == 0:
        source = DataSource.PROVIDER
    else:
        source = DataSource.MIXED

    if not candles:
        status = last_error_status or DataStatus.MISSING
    elif missing_days:
        status = last_error_status or DataStatus.PARTIAL
    else:
        status = DataStatus.AVAILABLE

    detail = None
    if missing_days:
        detail = f"{len(missing_days)} NSE trading day(s) in range have no data: {', '.join(d.isoformat() for d in missing_days[:5])}" + ("..." if len(missing_days) > 5 else "")

    return HistoricalDataResult(
        status=status, source=source, provider=settings.market_data_provider,
        canonical_id=ai_instrument.canonical_id, symbol=symbol, interval=interval,
        requested_from=start, requested_to=end, cutoff_applied=cutoff_dt,
        candles=candles, db_hit_count=db_hit_count, provider_call_count=provider_call_count,
        candles_fetched=candles_fetched, candles_stored=candles_stored,
        missing_days=tuple(missing_days), detail=detail,
    )
