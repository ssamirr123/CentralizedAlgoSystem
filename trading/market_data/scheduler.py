"""
Phase 4 -- centralized, timezone-aware market-data schedule.

    MARKET_DATA_START_TIME  (default 09:10 IST)  -> run the daily startup flow
    MARKET_DATA_STOP_TIME   (default 15:45 IST)  -> run the daily stop flow

The scheduler decides *when*; the actual startup/stop sequences (connect
Breeze, subscribe, start cache / aggregation / persistence, ... / flush,
close socket, ...) are the ``on_start`` / ``on_stop`` callables supplied
by the feed service in Phase 7+. Here they may be light stubs.

``decide()`` is pure and takes ``now`` + ``feed_running`` explicitly so
Phase 22 can test every boundary (08:59 / 09:10 / 15:45 / weekend /
holiday) without touching a real clock.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable

from trading.core.config import Settings, load_settings
from trading.market_data import market_hours as mh
from trading.market_data.status import FEED_STATUS, FeedState

logger = logging.getLogger("trading.market_data.scheduler")

Hook = Callable[[], Awaitable[None] | None]


class SchedulerAction(str, Enum):
    START = "START"      # in-window, feed not running -> run startup flow
    STOP = "STOP"        # out-of-window, feed running -> run stop flow
    RUNNING = "RUNNING"  # in-window, feed already running -> nothing to do
    IDLE = "IDLE"        # out-of-window, feed not running -> nothing to do


class MarketDataScheduler:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        on_start: Hook | None = None,
        on_stop: Hook | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_seconds: float = 30.0,
    ) -> None:
        self._settings = settings or load_settings()
        self._tz = mh.market_tz(self._settings.market_data_timezone)
        self._start = mh.parse_hhmm(self._settings.market_data_start_time)
        self._stop = mh.parse_hhmm(self._settings.market_data_stop_time)
        self._holidays = mh.parse_holidays(self._settings.market_data_holidays)
        self._on_start = on_start
        self._on_stop = on_stop
        self._clock = clock or (lambda: mh.now_in_tz(self._tz))
        self._poll = poll_seconds
        self._feed_running = False
        self._task: asyncio.Task | None = None
        self._stopping = False

    # --- pure decision ------------------------------------------------
    def is_open(self, now: datetime) -> bool:
        return mh.market_is_open(
            now, start=self._start, stop=self._stop, tz=self._tz, holidays=self._holidays
        )

    def decide(self, now: datetime, *, feed_running: bool) -> SchedulerAction:
        open_now = self.is_open(now)
        if open_now and not feed_running:
            return SchedulerAction.START
        if not open_now and feed_running:
            return SchedulerAction.STOP
        return SchedulerAction.RUNNING if open_now else SchedulerAction.IDLE

    def next_start(self, now: datetime) -> datetime | None:
        return mh.next_market_start(
            now, start=self._start, stop=self._stop, tz=self._tz, holidays=self._holidays
        )

    # --- one poll iteration ---------------------------------------
    async def tick(self, *, now: datetime | None = None) -> SchedulerAction:
        now = now or self._clock()
        action = self.decide(now, feed_running=self._feed_running)
        if action is SchedulerAction.START:
            await self._run_start(now)
        elif action is SchedulerAction.STOP:
            await self._run_stop(now)
        return action

    async def _run_start(self, now: datetime) -> None:
        logger.info("market_data.start scheduled=%s tz=%s", now.isoformat(), self._settings.market_data_timezone)
        FEED_STATUS.update(feed_state=FeedState.CONNECTING, last_error=None)
        try:
            await _maybe_await(self._on_start)
        except Exception as exc:  # noqa: BLE001 - stay alive, retry next poll
            logger.exception("market_data startup flow failed")
            FEED_STATUS.update(feed_state=FeedState.ERROR, last_error=f"startup failed ({type(exc).__name__})")
            return
        self._feed_running = True
        FEED_STATUS.update(feed_state=FeedState.RUNNING, started_at=now, stopped_at=None)

    async def _run_stop(self, now: datetime) -> None:
        logger.info("market_data.stop scheduled=%s", now.isoformat())
        FEED_STATUS.update(feed_state=FeedState.STOPPING)
        try:
            await _maybe_await(self._on_stop)
        except Exception as exc:  # noqa: BLE001
            logger.exception("market_data stop flow failed")
            FEED_STATUS.update(last_error=f"stop flow error ({type(exc).__name__})")
        self._feed_running = False
        FEED_STATUS.update(feed_state=FeedState.STOPPED, stopped_at=now)

    # --- background loop ------------------------------------------
    async def run(self) -> None:
        logger.info(
            "market_data.scheduler start window=%s-%s %s poll=%ss",
            self._settings.market_data_start_time, self._settings.market_data_stop_time,
            self._settings.market_data_timezone, self._poll,
        )
        self._stopping = False
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("market_data.scheduler tick error")
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                raise

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
        return self._task

    async def shutdown(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._feed_running:
            await self._run_stop(self._clock())

    @property
    def feed_running(self) -> bool:
        return self._feed_running


async def _maybe_await(hook: Hook | None) -> None:
    if hook is None:
        return
    result = hook()
    if inspect.isawaitable(result):
        await result
