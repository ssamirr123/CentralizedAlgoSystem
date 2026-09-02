"""Phase 4 / Phase 22 -- market-data scheduler (no real clock, no pytest-asyncio)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trading.core.config import load_settings
from trading.market_data.scheduler import MarketDataScheduler, SchedulerAction
from trading.market_data.status import FEED_STATUS, FeedState

IST = ZoneInfo("Asia/Kolkata")


def _sched(**kw):
    return MarketDataScheduler(settings=load_settings(), **kw)


@pytest.mark.parametrize(
    "hh,mm,feed_running,expected",
    [
        (8, 59, False, SchedulerAction.IDLE),
        (9, 9, False, SchedulerAction.IDLE),
        (9, 10, False, SchedulerAction.START),
        (9, 11, True, SchedulerAction.RUNNING),
        (15, 44, True, SchedulerAction.RUNNING),
        (15, 45, True, SchedulerAction.STOP),
        (15, 46, True, SchedulerAction.STOP),   # still running after close -> stop it
        (15, 46, False, SchedulerAction.IDLE),  # already stopped -> nothing to do
        (16, 0, False, SchedulerAction.IDLE),
    ],
)
def test_decide_boundaries_on_a_trading_day(hh, mm, feed_running, expected):
    s = _sched()
    now = datetime(2026, 9, 7, hh, mm, tzinfo=IST)  # Monday
    assert s.decide(now, feed_running=feed_running) == expected


def test_decide_weekend_never_starts_and_stops_if_running():
    s = _sched()
    sat_noon = datetime(2026, 9, 12, 12, 0, tzinfo=IST)
    assert s.decide(sat_noon, feed_running=False) == SchedulerAction.IDLE
    assert s.decide(sat_noon, feed_running=True) == SchedulerAction.STOP


def test_decide_holiday(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_HOLIDAYS", "2026-09-07")
    s = MarketDataScheduler(settings=load_settings())
    mon_noon = datetime(2026, 9, 7, 12, 0, tzinfo=IST)
    assert s.decide(mon_noon, feed_running=False) == SchedulerAction.IDLE


def test_tick_runs_start_then_stop_hooks_and_updates_status():
    calls = []
    s = _sched(on_start=lambda: calls.append("start"), on_stop=lambda: calls.append("stop"))

    async def _flow():
        a1 = await s.tick(now=datetime(2026, 9, 7, 9, 10, tzinfo=IST))
        a2 = await s.tick(now=datetime(2026, 9, 7, 15, 45, tzinfo=IST))
        return a1, a2

    a1, a2 = asyncio.run(_flow())
    assert (a1, a2) == (SchedulerAction.START, SchedulerAction.STOP)
    assert calls == ["start", "stop"]
    assert s.feed_running is False
    assert FEED_STATUS.snapshot()["feed_state"] == FeedState.STOPPED.value


def test_async_hooks_are_awaited():
    hit = []

    async def astart():
        await asyncio.sleep(0)
        hit.append(1)

    s = _sched(on_start=astart)
    asyncio.run(s.tick(now=datetime(2026, 9, 7, 9, 30, tzinfo=IST)))
    assert hit == [1] and s.feed_running is True


def test_start_hook_failure_sets_error_and_retries_next_tick():
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise RuntimeError("breeze down")

    s = _sched(on_start=flaky)
    open_dt = datetime(2026, 9, 7, 9, 30, tzinfo=IST)

    asyncio.run(s.tick(now=open_dt))
    assert s.feed_running is False
    assert FEED_STATUS.snapshot()["feed_state"] == FeedState.ERROR.value

    state["fail"] = False
    asyncio.run(s.tick(now=open_dt))  # retried because feed still not running
    assert s.feed_running is True
    assert FEED_STATUS.snapshot()["feed_state"] == FeedState.RUNNING.value


def test_run_loop_start_and_shutdown():
    calls = []

    async def _flow():
        s = _sched(
            on_start=lambda: calls.append("s"),
            on_stop=lambda: calls.append("x"),
            clock=lambda: datetime(2026, 9, 7, 10, 0, tzinfo=IST),
            poll_seconds=0.02,
        )
        s.start()
        await asyncio.sleep(0.12)
        running = s.feed_running
        await s.shutdown()
        return running, s.feed_running

    running, after = asyncio.run(_flow())
    assert running is True and "s" in calls
    assert after is False and "x" in calls
