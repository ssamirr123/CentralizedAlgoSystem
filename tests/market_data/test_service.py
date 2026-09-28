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


def test_stop_flow_drops_provider_so_next_start_rebuilds_it(db_session):
    """Regression: a Breeze session token is daily -- stop_flow() must not
    leave a stale provider (built with today's token) sitting around for
    the next startup_flow() to silently reuse tomorrow."""
    prov = FakeProvider()
    svc = _svc(provider=prov)
    svc._accepting = True
    asyncio.run(svc.stop_flow())
    assert svc._provider is None


def test_reset_provider_disconnects_and_clears(db_session):
    prov = FakeProvider()
    svc = _svc(provider=prov)
    prov.connect()
    svc.reset_provider()
    assert svc._provider is None
    assert prov.disconnects == 1


def test_reconnect_rebuilds_provider_from_current_credentials(db_session, monkeypatch):
    """Regression: after an admin posts a fresh session token, the live
    feed must rebuild its provider from it (not keep using whatever
    provider/credentials it was originally built with)."""
    old_prov = FakeProvider()
    svc = _svc(provider=old_prov)
    svc._accepting = True

    new_prov = FakeProvider()
    built_with: list = []

    def fake_factory(name, **kw):
        built_with.append(kw)
        return new_prov

    monkeypatch.setattr("trading.market_data.service.create_market_data_provider", fake_factory)

    async def flow():
        task = asyncio.create_task(svc.reconnect())
        for _ in range(20):
            await asyncio.sleep(0.02)
            if new_prov.on_tick is not None:
                break
        new_prov.push_index("NIFTY", 25050)
        await task

    asyncio.run(flow())

    assert svc._provider is new_prov  # rebuilt, not the original object
    assert old_prov.disconnects == 1  # the stale one was torn down
    assert built_with and built_with[0]["session_token"] == "t"  # FakeSession's current creds
    asyncio.run(svc.stop_flow())


class SlowFailProvider(FakeProvider):
    """connect() blocks (like Breeze's login + security-master download) and fails."""
    def __init__(self, block_seconds=0.3):
        super().__init__()
        self.block_seconds = block_seconds
        self.connect_calls = 0

    def connect(self):
        import time as _t
        self.connect_calls += 1
        _t.sleep(self.block_seconds)
        raise RuntimeError("breeze down")


def test_reconnect_does_not_block_event_loop(db_session, monkeypatch):
    """A slow, failing Breeze connect must run off the event loop, so other
    requests (algo heartbeats) keep being served during reconnect."""
    import time as _t
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep())
    prov = SlowFailProvider(block_seconds=0.3)
    svc = _svc(provider=prov)
    svc._accepting = True

    async def main():
        task = asyncio.create_task(svc._reconnect())
        gaps, last = [], _t.monotonic()
        while not task.done():
            await _real_sleep(0.01)
            now = _t.monotonic(); gaps.append(now - last); last = now
        await task
        return max(gaps)

    worst_gap = asyncio.run(main())
    assert prov.connect_calls == 5
    assert worst_gap < 0.2, f"event loop was blocked for {worst_gap:.2f}s"


def test_staleness_check_never_overlaps_reconnects_and_cools_down(db_session, monkeypatch):
    import trading.market_data.service as service_mod
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep())
    prov = SlowFailProvider(block_seconds=0.05)
    svc = _svc(provider=prov)
    svc._accepting = True
    svc.cache._last_tick_at = NOW.replace(hour=0)         # very stale (clock is NOW)

    async def main():
        for _ in range(20):                               # 20 flush ticks while stale
            svc._check_staleness()
            await _real_sleep(0.01)
        await svc._reconnect_task
        first = prov.connect_calls
        for _ in range(5):                                # still stale, but cooling down
            svc._check_staleness()
            await _real_sleep(0.01)
        return first

    first = asyncio.run(main())
    assert first == 5, f"expected one reconnect loop (5 attempts), got {first} connects"
    assert prov.connect_calls == 5, "a new loop started during the cool-down"
    assert svc._reconnect_not_before > 0
    assert service_mod._RECONNECT_COOLDOWN_SECONDS == 300


_real_sleep = asyncio.sleep


def _fast_sleep():
    async def fast(delay, *a, **k):
        return await _real_sleep(0)
    return fast
