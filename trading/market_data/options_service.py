"""
Phase 9 -- Options Intelligence acquisition/persistence glue.

Bridges the deterministic ``options_intelligence`` calculations to real
data sources:

  * LIVE  chain  -> two bounded on-demand Breeze REST calls, one per
                    option side (``ICICIBreezeProvider.get_option_chain``
                    with ``right="CE"``/``"PE"`` -- the real Breeze API
                    rejects a request with both ``right`` and
                    ``strike_price`` empty), merged and trimmed to a
                    strike window in-process (no per-strike requests).
  * LIVE  chain  -> or, when the streaming service already has it, the
                    in-memory ``LiveCache`` (zero additional provider
                    calls) via the existing ``option_chain.build_option_chain``.
  * HISTORICAL chain (``as_of`` given) -> persisted ``OptionCandle`` rows
                    only, the latest candle per contract with
                    ``timestamp <= as_of`` (Section 34/35: never touches
                    Breeze for a historical request, never lets a future
                    row leak in).

Options Intelligence never calls Breeze directly from React or from
TradingAgents -- both reach it only through this module's functions or
the API router built on top of them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.core.config import Settings, load_settings
from trading.market_data import option_chain as oc
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.models import OptionCandle, OptionContract
from trading.market_data.providers import create_market_data_provider
from trading.market_data.schemas import OptionChain, OptionChainRow, OptionQuote


class OptionsDataError(RuntimeError):
    """Raised for a caller-facing failure (unknown underlying/expiry, no
    data at all) -- distinct from a deterministic-but-empty analytics
    result, which is returned as a normal (low-quality) value instead."""


@dataclass(frozen=True)
class ChainResult:
    chain: OptionChain
    source: str          # "ICICI_BREEZE" | "LIVE_CACHE" | "TIMESCALEDB"
    provider_call_count: int


def get_live_chain_via_provider(
    underlying: str, expiry: date, *, strike_window: int, settings: Settings | None = None,
    provider_factory=None,
) -> ChainResult:
    """On-demand REST fetch of the full chain, trimmed to ATM +/-
    strike_window afterwards. Section 3 finding (verified against the
    real live Breeze API, not assumed): ``get_option_chain_quotes``
    actually REJECTS a request with both ``right=""`` and
    ``strike_price=""`` ("Either Right or Strike-Price cannot be empty"),
    contrary to what Stage 19's own single-call ``ICICIBreezeProvider.
    get_option_chain`` assumes when called with no ``right`` filter. This
    function works around that real contract constraint by issuing one
    call per side (``right="CE"``, ``right="PE"``) and merging by strike
    -- two Breeze calls total for the whole chain (Section 52: reported
    exactly via ``provider_call_count``, no per-strike concurrency)."""
    if provider_factory is None:
        provider_factory = create_market_data_provider
    settings = settings or load_settings()
    provider = provider_factory(
        settings.market_data_provider,
        api_key=settings.breeze_api_key, api_secret=settings.breeze_secret_key,
        session_token=settings.breeze_session_token,
    )
    provider.connect()
    try:
        call_chain = provider.get_option_chain(underlying, expiry, right="CE")
        put_chain = provider.get_option_chain(underlying, expiry, right="PE")
    finally:
        provider.disconnect()

    by_strike: dict[float, dict[str, object]] = {}
    for row in call_chain.rows:
        if row.call is not None:
            by_strike.setdefault(row.strike, {})["call"] = row.call
    for row in put_chain.rows:
        if row.put is not None:
            by_strike.setdefault(row.strike, {})["put"] = row.put

    spot = call_chain.spot if call_chain.spot is not None else put_chain.spot
    strikes = sorted(by_strike)
    atm = call_chain.atm_strike or put_chain.atm_strike or (oc.nearest_strike(strikes, spot) if spot else None)
    window = oc.select_strike_window(strikes, atm, strike_window) if atm is not None else strikes

    merged_rows = [
        OptionChainRow(strike=s, call=by_strike[s].get("call"), put=by_strike[s].get("put"))
        for s in window
    ]
    merged = OptionChain(
        underlying=underlying.upper(), expiry=expiry, spot=spot, atm_strike=atm,
        generated_at=call_chain.generated_at, rows=merged_rows, provider=call_chain.provider,
    )
    return ChainResult(chain=merged, source="ICICI_BREEZE", provider_call_count=2)


def get_live_chain_from_cache(
    underlying: str, expiry: date, *, strike_window: int, cache: LiveCache, master: InstrumentMaster,
) -> ChainResult:
    """Zero-provider-call read from the already-streaming ``LiveCache``
    (populated continuously by the WS feed, if running). Rows the cache
    has no quote for yet come back as null call/put -- never fabricated."""
    spot_entry = cache.get_latest_quote(underlying.upper())
    spot = spot_entry.quote.ltp if spot_entry else None
    chain = oc.build_option_chain(
        underlying=underlying, expiry=expiry, spot=spot, cache=cache, master=master, strike_range=strike_window,
    )
    return ChainResult(chain=chain, source="LIVE_CACHE", provider_call_count=0)


def get_historical_chain(
    db: Session, underlying: str, expiry: date, as_of: datetime, *, strike_window: int | None = None,
) -> ChainResult:
    """The latest persisted ``OptionCandle`` per contract with
    ``timestamp <= as_of`` -- built from Stage 19's existing 1-minute
    option-candle persistence, never from Breeze (Section 34: point-in-time
    historical support must never touch a live provider)."""
    under = underlying.strip().upper()
    contracts = db.execute(
        select(OptionContract).where(OptionContract.underlying == under, OptionContract.expiry == expiry)
    ).scalars().all()
    if not contracts:
        raise OptionsDataError(f"No persisted contracts for {under} expiry {expiry.isoformat()}")

    by_strike: dict[float, dict[str, OptionQuote]] = {}
    latest_ts: datetime | None = None
    for contract in contracts:
        latest = db.execute(
            select(OptionCandle)
            .where(OptionCandle.contract_id == contract.id, OptionCandle.timestamp <= as_of)
            .order_by(OptionCandle.timestamp.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            continue
        latest_ts = latest.timestamp if (latest_ts is None or latest.timestamp > latest_ts) else latest_ts
        quote = OptionQuote.build(
            underlying=under, expiry=expiry, strike=contract.strike, option_type=contract.option_type,
            ltp=latest.close, open=latest.open, high=latest.high, low=latest.low,
            volume=latest.volume, oi=latest.oi, provider_timestamp=latest.timestamp, provider="timescaledb",
        )
        by_strike.setdefault(contract.strike, {})[contract.option_type] = quote

    if not by_strike:
        raise OptionsDataError(f"No persisted candle at/before {as_of.isoformat()} for {under} expiry {expiry.isoformat()}")

    strikes = sorted(by_strike)
    spot = _historical_spot(db, under, as_of)
    atm = oc.nearest_strike(strikes, spot) if spot is not None else None
    window = oc.select_strike_window(strikes, atm, strike_window) if (atm is not None and strike_window is not None) else strikes

    rows = [
        OptionChainRow(strike=s, call=by_strike[s].get("CE"), put=by_strike[s].get("PE"))
        for s in window
    ]
    chain = OptionChain(
        underlying=under, expiry=expiry, spot=spot, atm_strike=atm,
        generated_at=latest_ts or as_of, rows=rows, provider="timescaledb",
    )
    return ChainResult(chain=chain, source="TIMESCALEDB", provider_call_count=0)


def _historical_spot(db: Session, underlying: str, as_of: datetime) -> float | None:
    from trading.market_data.models import MarketCandle

    row = db.execute(
        select(MarketCandle)
        .where(MarketCandle.symbol == underlying, MarketCandle.timestamp <= as_of)
        .order_by(MarketCandle.timestamp.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.close if row is not None else None


def build_options_market_context(underlying: str, research_date: date) -> tuple[str | None, dict]:
    """Section 46: a deterministic FACTS-ONLY text block for AI Research to
    read (ATM IV / PCR / Max Pain / OI levels / Expected Move) -- never a
    strategy suggestion (Section 47 boundary enforced upstream: nothing
    here is an LLM call, and the caller must not ask an LLM to compute any
    of these numbers, only to read them).

    Only attached for a LIVE/current research date -- Options Intelligence
    has no broad historical option-chain backfill yet (Section 48: no
    uncontrolled historical download), so honoring point-in-time safety
    for an arbitrary past research_date would otherwise require silently
    falling back to today's chain, which Section 34 explicitly forbids.
    A past research_date simply gets no options context (documented
    limitation), never today's chain mislabeled as historical."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from trading.market_data.options_intelligence import build_market_structure_summary
    from trading.market_data.service import get_service

    provenance = {"options_context_included": False, "underlying": underlying.upper()}
    if research_date != _dt.now(_tz.utc).date():
        provenance["reason"] = "options context is only attached for a live/current research date"
        return None, provenance

    try:
        settings = load_settings()
        master = get_service().master
        expiries = master.list_expiries(underlying)
        expiry = oc.resolve_expiry(master, underlying, "current")
        if expiry is None:
            provenance["reason"] = "no expiries known yet for this underlying"
            return None, provenance
        result = get_live_chain_from_cache(
            underlying, expiry, strike_window=settings.options_chain_strike_window,
            cache=get_service().cache, master=master,
        )
        if not any(r.call is not None or r.put is not None for r in result.chain.rows):
            result = get_live_chain_via_provider(underlying, expiry, strike_window=settings.options_chain_strike_window, settings=settings)
        summary = build_market_structure_summary(
            result.chain, as_of=None, risk_free_rate=settings.options_risk_free_rate,
            all_expiries=expiries or [expiry], underlying_source=result.source,
            option_chain_source=result.source, provider_call_count=result.provider_call_count,
        )
    except Exception as exc:  # noqa: BLE001 -- options-context unavailability must never block research
        provenance["reason"] = f"options context unavailable ({type(exc).__name__})"
        return None, provenance

    lines = [
        f"## Options Market Context ({summary.underlying}, expiry {summary.expiry})",
        f"Spot: {summary.spot}  ATM strike: {summary.atm_strike}",
        f"OI PCR: {summary.pcr.oi_pcr}  Volume PCR: {summary.pcr.volume_pcr}",
        f"Max Pain strike: {summary.max_pain.max_pain_strike}",
        f"OI-derived support (highest Put OI): {list(summary.support_resistance.oi_support)}",
        f"OI-derived resistance (highest Call OI): {list(summary.support_resistance.oi_resistance)}",
        f"ATM IV: {summary.atm_iv} (source: {summary.atm_iv_source.value})",
        f"Expected move ({summary.expected_move.method}): {summary.expected_move.expected_move}",
        f"Data quality: {summary.quality.status.value}",
        "These are deterministic facts/calculations only -- no options-strategy recommendation.",
    ]
    provenance["options_context_included"] = True
    provenance["options_quality"] = summary.quality.status.value
    return "\n".join(lines), provenance


def persist_option_chain_snapshot(db: Session, chain: OptionChain) -> int:
    """Idempotent: get-or-create the OptionContract natural key, then
    upsert-by-timestamp the OptionCandle row (unique on contract_id+ts).
    Re-ingesting the identical snapshot is a no-op update, never a
    duplicate row (Section 13)."""
    written = 0
    for row in chain.rows:
        for ot, quote in (("CE", row.call), ("PE", row.put)):
            if quote is None:
                continue
            contract = db.execute(
                select(OptionContract).where(
                    OptionContract.underlying == chain.underlying,
                    OptionContract.exchange == "NFO",
                    OptionContract.expiry == chain.expiry,
                    OptionContract.strike == row.strike,
                    OptionContract.option_type == ot,
                )
            ).scalar_one_or_none()
            if contract is None:
                contract = OptionContract(
                    underlying=chain.underlying, exchange="NFO", provider=chain.provider or "icici_breeze",
                    symbol=quote.symbol, expiry=chain.expiry, strike=row.strike, option_type=ot,
                )
                db.add(contract)
                db.flush()

            # Section 3 finding: Breeze's own exchange timestamp (ltt) is
            # real IST wall-clock time, but Stage 19's shared _dt() helper
            # (icici_breeze.py) labels it tzinfo=UTC without converting --
            # a pre-existing mislabeling bug outside this phase's scope
            # (fixing it would touch every other consumer of that helper).
            # Deliberately NOT using quote.provider_timestamp as the
            # candle key here; chain.generated_at is this module's own
            # correctly-UTC-aware capture time (Section 32/35 depend on
            # this timestamp being trustworthy for ordering/cutoff).
            ts = chain.generated_at
            existing = db.execute(
                select(OptionCandle).where(OptionCandle.contract_id == contract.id, OptionCandle.timestamp == ts)
            ).scalar_one_or_none()
            if existing is not None:
                existing.close = quote.ltp if quote.ltp is not None else existing.close
                existing.oi = quote.oi if quote.oi is not None else existing.oi
                existing.volume = quote.volume if quote.volume is not None else existing.volume
                continue
            price = quote.ltp if quote.ltp is not None else 0.0
            db.add(OptionCandle(
                timestamp=ts, contract_id=contract.id,
                open=price, high=price, low=price, close=price,
                volume=quote.volume, oi=quote.oi,
            ))
            written += 1
    db.commit()
    return written
