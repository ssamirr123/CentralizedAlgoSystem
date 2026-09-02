"""
Phase 4 wiring + Phase 16 recovery -- the market-data feed service.

Owns the provider, live cache, 1-minute aggregator and instrument master.
The scheduler (scheduler.py) calls ``on_start`` / ``on_stop``; this module
runs the actual daily startup / stop sequences and the tick pipeline:

    Breeze tick -> live cache -> 1-min aggregator -> PostgreSQL
                             -> throttled realtime publish (market_quote)

Failure handling (Phase 16): bounded exponential-backoff reconnect during
market hours; a bad tick never kills the socket thread; every transition
is logged (market_data.* / breeze.*) and mirrored to FEED_STATUS.

No order-placement path exists here (Phase 29).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from trading.core.config import Settings, load_settings
from trading.market_data import option_chain as oc
from trading.market_data.aggregator import (
    CandleAggregator,
    persist_index_candles,
    persist_option_candles,
)
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster, get_instrument_master
from trading.market_data.providers import ProviderError, create_market_data_provider
from trading.market_data.schemas import IndexQuote, OptionQuote
from trading.market_data.session import BreezeSessionManager, get_session_manager
from trading.market_data.status import FEED_STATUS, FeedState, SessionState
from trading.market_data.symbols import (
    INDEX_INSTRUMENTS,
    Instrument,
    index_instrument,
    make_option_symbol,
)

logger = logging.getLogger("trading.market_data.service")

_INDEX_SUBSCRIBE = ("NIFTY", "BANKNIFTY", "INDIA_VIX", "SENSEX")


class MarketDataService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_manager: BreezeSessionManager | None = None,
        provider=None,
        cache: LiveCache | None = None,
        aggregator: CandleAggregator | None = None,
        instrument_master: InstrumentMaster | None = None,
        session_factory=None,
        publisher=None,
        clock=None,
        flush_interval: float = 15.0,
        first_tick_timeout: float = 20.0,
    ) -> None:
        self.settings = settings or load_settings()
        self._sm = session_manager or get_session_manager()
        self._provider = provider
        self.cache = cache or LiveCache(stale_seconds=self.settings.market_data_stale_seconds)
        self.aggregator = aggregator or CandleAggregator()
        self.master = instrument_master or get_instrument_master()
        self._session_factory = session_factory or _default_session_factory
        self._publish = publisher or _default_publisher()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._flush_interval = flush_interval
        self._first_tick_timeout = first_tick_timeout

        self._accepting = False
        self._flush_task: asyncio.Task | None = None
        self._contract_ids: dict[str, int] = {}      # internal option symbol -> option_contracts.id
        self._option_universe: set[str] = set()
        self._last_publish: dict[str, float] = {}
        self._reconnects = 0

    # --- scheduler hooks --------------------------------------------
    async def on_start(self) -> None:
        await self.startup_flow()

    async def on_stop(self) -> None:
        await self.stop_flow()

    # --- daily startup (15 steps) --------------------------------
    async def startup_flow(self) -> None:
        s = self.settings
        logger.info("market_data.start begin tz=%s", s.market_data_timezone)

        # 2. verify Breeze session
        chk = self._sm.check()
        if chk.state is not SessionState.VALID:
            FEED_STATUS.update(feed_state=FeedState.SESSION_REQUIRED, last_error=chk.detail)
            raise RuntimeError(f"Breeze session not usable: {chk.state.value}")

        # 3. init Breeze provider
        if self._provider is None:
            creds = self._sm.credentials()
            self._provider = create_market_data_provider(
                s.market_data_provider, api_key=creds.api_key,
                api_secret=creds.secret_key, session_token=creds.session_token,
            )
        if not self._provider.is_connected():
            self._provider.connect()

        # 4-5. connect WS + subscribe indices
        self._accepting = True
        index_insts = [INDEX_INSTRUMENTS[s_] for s_ in _INDEX_SUBSCRIBE]
        self._provider.subscribe(index_insts, self._on_tick, resolver=self._resolve_tick)

        # 6. load / refresh instrument master + persist contracts
        try:
            self.master.refresh(self._provider, ("NIFTY",), as_of=self._clock().astimezone(_ist(s)).date())
            self._persist_contracts()
        except ProviderError as exc:
            logger.warning("market_data.instrument_master refresh failed: %s", type(exc).__name__)

        # 7-11. spot -> ATM -> expiries -> strike universe -> subscribe options
        await self._wait_first_tick()
        self._resubscribe_option_universe()

        # 13-14. aggregation + persistence loop
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

        # 15. RUNNING only once a real tick has landed
        if self.cache.last_tick_at is not None:
            FEED_STATUS.update(feed_state=FeedState.RUNNING, started_at=self._clock(), last_error=None,
                               reconnect_count=self._reconnects)
            self._publish_status(FeedState.RUNNING)
            logger.info("market_data.start done symbols_live=%d options=%d",
                        self.cache.symbols_live(), len(self._option_universe))
        else:
            FEED_STATUS.update(feed_state=FeedState.CONNECTING)
            logger.warning("market_data.start no tick within %ss -- staying CONNECTING", self._first_tick_timeout)

    # --- daily stop (8 steps) ----------------------------------
    async def stop_flow(self) -> None:
        logger.info("market_data.stop begin")
        FEED_STATUS.update(feed_state=FeedState.STOPPING)
        self._accepting = False  # 1-2. stop new subs / stop accepting ticks

        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        # 3-4. complete aggregation + flush pending writes
        idx, opt = self.aggregator.force_close_all(self._clock())
        self._persist(idx, opt)

        # 5. close Breeze
        if self._provider is not None:
            try:
                self._provider.disconnect()
            except Exception:  # noqa: BLE001
                pass

        # 6. clear cache
        stats = {"symbols_seen": len(self.cache.all_indices()) + len(self.cache.all_options()),
                 "reconnects": self._reconnects}
        self.cache.clear()
        self._contract_ids.clear()
        self._option_universe.clear()

        # 7-8. mark STOPPED + daily stats
        FEED_STATUS.update(feed_state=FeedState.STOPPED, stopped_at=self._clock())
        self._publish_status(FeedState.STOPPED)
        logger.info("market_data.stop done stats=%s", stats)

    # --- tick pipeline ----------------------------------------
    def _on_tick(self, quote: IndexQuote | OptionQuote) -> None:
        if not self._accepting:
            return
        try:
            self.cache.put(quote)
            now = self._clock()
            if isinstance(quote, IndexQuote) and quote.ltp is not None:
                inst = INDEX_INSTRUMENTS.get(quote.symbol)
                self.aggregator.on_index_tick(
                    quote.symbol, inst.exchange.value if inst else "NSE", quote.ltp,
                    quote.provider_timestamp or now, volume=quote.volume,
                )
                self._maybe_publish_quote(quote, "index")
            elif isinstance(quote, OptionQuote) and quote.ltp is not None:
                cid = self._contract_ids.get(quote.symbol)
                if cid is not None:
                    self.aggregator.on_option_tick(
                        cid, quote.ltp, quote.provider_timestamp or now,
                        volume=quote.volume, oi=quote.oi,
                    )
                self._maybe_publish_quote(quote, "option")
            FEED_STATUS.update(last_tick_at=now, symbols_live=self.cache.symbols_live(now))
            if FEED_STATUS.feed_state is FeedState.CONNECTING:
                FEED_STATUS.update(feed_state=FeedState.RUNNING, started_at=now)
                self._publish_status(FeedState.RUNNING)
        except Exception:  # noqa: BLE001 - a bad tick must never kill the socket thread
            logger.debug("market_data tick handling error", exc_info=True)

    def _maybe_publish_quote(self, q: IndexQuote | OptionQuote, kind: str) -> None:
        interval = self.settings.market_ws_update_interval_ms / 1000.0
        now = time.monotonic()
        if now - self._last_publish.get(q.symbol, 0.0) < interval:
            return
        self._last_publish[q.symbol] = now
        try:
            self._publish.market_quote(
                q.symbol, ltp=q.ltp, change=q.change, change_percent=q.change_percent,
                kind=kind, timestamp=(q.provider_timestamp or self._clock()).isoformat(),
            )
        except Exception:  # noqa: BLE001
            pass

    def _publish_status(self, feed_state: FeedState) -> None:
        try:
            self._publish.market_status(
                feed_state=feed_state.value, session_state=self._sm.state().value,
                detail={"reconnect_count": self._reconnects},
            )
        except Exception:  # noqa: BLE001
            pass

    # --- flush / persistence loop -----------------------------
    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                idx, opt = self.aggregator.flush(self._clock())
                self._persist(idx, opt)
                self._check_staleness()
                self._resubscribe_option_universe()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("market_data.flush loop error")

    def _persist(self, idx: list, opt: list) -> None:
        if not idx and not opt:
            return
        db = self._session_factory()
        try:
            n1 = persist_index_candles(db, idx)
            n2 = persist_option_candles(db, opt)
            if n1 or n2:
                logger.info("market_data.persist index_candles=%d option_candles=%d", n1, n2)
        except Exception:  # noqa: BLE001
            logger.exception("market_data.database_error persisting candles")
            db.rollback()
        finally:
            db.close()

    def _check_staleness(self) -> None:
        last = self.cache.last_tick_at
        now = self._clock()
        if last is None:
            return
        age = (now - last).total_seconds()
        if age > self.settings.market_data_stale_seconds and FEED_STATUS.feed_state is FeedState.RUNNING:
            FEED_STATUS.update(feed_state=FeedState.STALE)
            self._publish_status(FeedState.STALE)
            logger.warning("market_data.stale last_tick_age=%.0fs", age)
        if age > 3 * self.settings.market_data_stale_seconds:
            asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        if not self._accepting or self._provider is None:
            return
        delay = 1.0
        for attempt in range(1, 6):
            try:
                self._provider.disconnect()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(delay)
            try:
                self._provider.connect()
                index_insts = [INDEX_INSTRUMENTS[s_] for s_ in _INDEX_SUBSCRIBE]
                self._provider.subscribe(index_insts, self._on_tick, resolver=self._resolve_tick)
                self._resubscribe_option_universe()
                self._reconnects += 1
                FEED_STATUS.update(reconnect_count=self._reconnects, feed_state=FeedState.RUNNING)
                logger.info("breeze.reconnected attempt=%d", attempt)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("breeze.reconnect_failed attempt=%d err=%s", attempt, type(exc).__name__)
                delay = min(delay * 2, 30.0)
        FEED_STATUS.update(feed_state=FeedState.ERROR, last_error="reconnect attempts exhausted")

    # --- option universe -------------------------------------
    def _persist_contracts(self) -> None:
        from trading.database import models

        insts = self.master._by_symbol.values()  # noqa: SLF001 - internal, read-only
        db = self._session_factory()
        try:
            existing = {c.symbol: c.id for c in db.query(models.OptionContract).all()}
            for inst in insts:
                if inst.internal_symbol in existing:
                    self._contract_ids[inst.internal_symbol] = existing[inst.internal_symbol]
                    continue
                row = models.OptionContract(
                    underlying=inst.underlying, exchange=inst.exchange.value, provider=inst.provider or "icici_breeze",
                    provider_token=inst.provider_token, symbol=inst.internal_symbol,
                    expiry=inst.expiry, strike=float(inst.strike), option_type=inst.option_type,
                    lot_size=inst.lot_size, tick_size=inst.tick_size,
                )
                db.add(row)
                db.flush()
                self._contract_ids[inst.internal_symbol] = row.id
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("market_data.database_error persisting option_contracts")
        finally:
            db.close()

    def _current_option_window(self) -> list[Instrument]:
        spot_entry = self.cache.get_latest_quote("NIFTY")
        spot = spot_entry.quote.ltp if spot_entry else None
        expiry = oc.resolve_expiry(self.master, "NIFTY", "current")
        if spot is None or expiry is None:
            return []
        strikes = self.master.list_strikes("NIFTY", expiry)
        atm = oc.nearest_strike(strikes, spot)
        window = oc.select_strike_window(strikes, atm, self.settings.nifty_option_strike_range)
        out: list[Instrument] = []
        for strike in window:
            for ot in ("CE", "PE"):
                inst = self.master.resolve("NIFTY", expiry, strike, ot)
                if inst is not None:
                    out.append(inst)
        return out

    def _resubscribe_option_universe(self) -> None:
        if self._provider is None or not self._accepting:
            return
        wanted = self._current_option_window()
        wanted_syms = {i.internal_symbol for i in wanted}
        add = [i for i in wanted if i.internal_symbol not in self._option_universe]
        drop_syms = self._option_universe - wanted_syms
        if add:
            try:
                self._provider.subscribe(add, self._on_tick, resolver=self._resolve_tick)
            except Exception:  # noqa: BLE001
                logger.warning("market_data option subscribe failed")
        if drop_syms:
            drop = [self.master.get(s) for s in drop_syms if self.master.get(s) is not None]
            try:
                self._provider.unsubscribe(drop)
            except Exception:  # noqa: BLE001
                pass
        self._option_universe = wanted_syms
        FEED_STATUS.update(option_contracts_subscribed=len(wanted_syms))

    def _resolve_tick(self, tick: dict) -> Instrument | None:
        """Map a raw option tick payload back to a known contract."""
        try:
            underlying = str(tick.get("stock_code") or tick.get("stock_name") or "NIFTY").upper()
            right = str(tick.get("right") or tick.get("option_type") or "").lower()
            ot = {"call": "CE", "ce": "CE", "put": "PE", "pe": "PE"}.get(right)
            strike = tick.get("strike_price") or tick.get("strike")
            expiry_raw = tick.get("expiry_date") or tick.get("expiry")
            if not (ot and strike and expiry_raw):
                return None
            from trading.market_data.providers.icici_breeze import _parse_expiry

            expiry = _parse_expiry(expiry_raw)
            if expiry is None:
                return None
            sym = make_option_symbol(underlying, expiry, float(strike), ot)
            return self.master.get(sym)
        except Exception:  # noqa: BLE001
            return None

    async def _wait_first_tick(self) -> None:
        deadline = time.monotonic() + self._first_tick_timeout
        while time.monotonic() < deadline:
            if self.cache.last_tick_at is not None:
                return
            await asyncio.sleep(0.25)


# --- process singleton ---------------------------------------------------
_service: MarketDataService | None = None


def get_service() -> MarketDataService:
    global _service
    if _service is None:
        _service = MarketDataService()
    return _service


def set_service(svc: MarketDataService | None) -> None:
    global _service
    _service = svc


def _default_session_factory():
    from trading.database.connection import SessionLocal

    return SessionLocal()


def _default_publisher():
    from trading.api.realtime import publish

    return publish


def _ist(settings: Settings):
    from zoneinfo import ZoneInfo

    return ZoneInfo(settings.market_data_timezone)
