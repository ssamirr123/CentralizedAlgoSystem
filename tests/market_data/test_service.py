"""Phase 4/16/20 -- feed service with a fully mocked provider (no network)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from trading.core.config import load_settings
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.schemas import IndexQuote
from trading.market_data.service import MarketDataService
from trading.market_data.status import FEED_STATUS, FeedState, SessionCheck, SessionState
from trading.market_data.symbols import option_instrument

NOW = datetime(2026, 9, 7, 9, 30, tzinfo=timezone.utc)


class FakeProvider:
    name = "icici_breeze"

    def __init__(self, *, connect_fail=False):
        self._connected = False
        self._connect_fail = connect_fail
        self.on_tick = None
        self.subscribed: list = []
        self.disconnects = 0

    def connect(self):
        if self._connect_fail:
            raise RuntimeError("no route")
        self._connected = True

    def disconnect(self):
        self._connected = False
        self.disconnects += 1

    def is_connected(self):
        return self._connected

    def subscribe(self, instruments, on_tick, *, resolver=None):
        self.on_tick = on_tick
        self.subscribed.extend(getattr(i, "internal_symbol", i) for i in instruments)

    def unsubscribe(self, instruments):
        for i in instruments:
            s = getattr(i, "internal_symbol", i)
            if s in self.subscribed:
                self.subscribed.remove(s)

    def get_option_instruments(self, underlying):
        return [
            option_instrument("NIFTY", date(2100, 9, 3), k, ot, lot_size=75, tick_size=0.05,
                              provider="icici_breeze", provider_token=f"t{k}{ot}")
            for k in range(24800, 25201, 50)
            for ot in ("CE", "PE")
        ]

    # test helper
    def push_index(self, symbol, ltp):
        self.on_tick(IndexQuote.build(symbol, ltp=ltp, prev_close=ltp - 5, provider=self.name))


class FakeSession:
    def __init__(self, state=SessionState.VALID):
        self._state = state

    def check(self, **_):
        return SessionCheck(self._state, NOW, "ok")

    def state(self):
        return self._state

    def credentials(self):
        from trading.market_data.session import BreezeCredentials

        return BreezeCredentials("k", "s", "t", "env")


class FakePublisher:
    def __init__(self):
        self.quotes = []
        self.statuses = []

    def market_quote(self, symbol, **kw):
        self.quotes.append((symbol, kw))

    def market_status(self, **kw):
        self.statuses.append(kw)


def _svc(provider=None, session=None, publisher=None):
    from trading.database.connection import SessionLocal

    return MarketDataService(
        settings=load_settings(),
        session_manager=session or FakeSession(),
        provider=provider or FakeProvider(),
        cache=LiveCache(stale_seconds=10),
        instrument_master=InstrumentMaster(),
        session_factory=lambda: SessionLocal(),
        publisher=publisher or FakePublisher(),
        clock=lambda: NOW,
        flush_interval=0.05,
        first_tick_timeout=0.3,
    )


def test_startup_requires_valid_session():
    svc = _svc(session=FakeSession(SessionState.SESSION_REQUIRED))
    with pytest.raises(RuntimeError):
        asyncio.run(svc.startup_flow())
    assert FEED_STATUS.snapshot()["feed_state"] == FeedState.SESSION_REQUIRED.value
    asyncio.run(svc.stop_flow())


def test_startup_subscribes_indices_and_goes_running_after_tick(db_session):
    prov = FakeProvider()
    pub = FakePublisher()
    svc = _svc(provider=prov, publisher=pub)

    async def flow():
        task = asyncio.create_task(svc.startup_flow())
        for _ in range(20):
            await asyncio.sleep(0.02)
            if prov.on_tick is not None:
                break
        prov.push_index("NIFTY", 25010)
        await task

    asyncio.run(flow())
    assert "NIFTY" in prov.subscribed and "SENSEX" in prov.subscribed
    assert svc.cache.get_latest_quote("NIFTY").quote.ltp == 25010
    assert FEED_STATUS.snapshot()["feed_state"] == FeedState.RUNNING.value
    assert any(sym == "NIFTY" for sym, _ in pub.quotes)
    asyncio.run(svc.stop_flow())


def test_tick_feeds_aggregator_and_cache(db_session):
    prov = FakeProvider()
    svc = _svc(provider=prov)
    svc._accepting = True
    svc._on_tick(IndexQuote.build("BANKNIFTY", ltp=52000, prev_close=51900, provider="icici_breeze"))
    assert svc.cache.get_latest_quote("BANKNIFTY").quote.ltp == 52000
    idx, _ = svc.aggregator.flush(NOW.replace(minute=NOW.minute + 1))
    assert any(sym == "BANKNIFTY" for sym, _, _ in idx)


def test_bad_tick_does_not_raise():
    svc = _svc()
    svc._accepting = True
    svc._on_tick("not-a-quote")            # type: ignore[arg-type]  -> swallowed
    svc._on_tick(IndexQuote.build("NIFTY", ltp=None))  # no ltp -> nothing aggregated
    idx, _ = svc.aggregator.flush(NOW.replace(minute=NOW.minute + 2))
    assert idx == []


def test_stop_flow_persists_and_clears(db_session):
    prov = FakeProvider()
    svc = _svc(provider=prov)
    svc._accepting = True
    svc._on_tick(IndexQuote.build("NIFTY", ltp=25000, prev_close=24950, provider="icici_breeze"))
    asyncio.run(svc.stop_flow())
    assert svc.cache.get_latest_quote("NIFTY") is None
    assert prov.disconnects == 1
    assert FEED_STATUS.snapshot()["feed_state"] == FeedState.STOPPED.value


def test_reconnect_after_provider_drop(db_session):
    prov = FakeProvider()
    svc = _svc(provider=prov)
    svc._accepting = True
    asyncio.run(svc._reconnect())
    assert prov.is_connected() is True
    assert FEED_STATUS.snapshot()["reconnect_count"] >= 1
