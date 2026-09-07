"""
Straddle Pulse engine -- ties the generic cycle/session/ATM/OI services
together per underlying. Composed alongside ``MarketDataService`` (not
merged into it) so this feature stays bounded and independently testable.

    recover(underlying)  -- called once per underlying from
                             MarketDataService.startup_flow(), after the
                             instrument master refresh. Implements the
                             spec's failure-recovery sequence: find/ensure
                             the active cycle, find/ensure today's session,
                             and lock ATM immediately if it is already due
                             (e.g. the process was down at 09:16).

    tick(underlying, now_ist) -- called from MarketDataService's flush
                             loop every ``flush_interval`` seconds. Same
                             idempotent operations; safe to call as often
                             as the caller likes (spec section 31).

Every operation here is idempotent by construction (unique DB
constraints + get-or-create), so duplicate scheduler runs or repeated
calls never create duplicate rows (spec section 31, Test 9).
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from trading.market_data.atm_lock import ATMSelectionService
from trading.market_data.cache import LiveCache
from trading.market_data.daily_session import DailySessionService
from trading.market_data.expiry_cycle import ExpiryCycleService
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.market_hours import parse_holidays
from trading.market_data.oi_pcr import OIService
from trading.market_data.underlying_config import STRADDLE_PULSE_UNDERLYINGS, underlying_config

logger = logging.getLogger("trading.market_data.straddle_pulse")


class StraddlePulseEngine:
    def __init__(
        self,
        *,
        master: InstrumentMaster,
        cache: LiveCache,
        session_factory,
        settings,
        cycles: ExpiryCycleService | None = None,
        sessions: DailySessionService | None = None,
        atm: ATMSelectionService | None = None,
        oi: OIService | None = None,
    ) -> None:
        self.master = master
        self.cache = cache
        self._session_factory = session_factory
        self.settings = settings
        self.cycles = cycles or ExpiryCycleService()
        self.sessions = sessions or DailySessionService()
        self.atm = atm or ATMSelectionService()
        self.oi = oi or OIService()
        self._tz = ZoneInfo(settings.market_data_timezone)
        self._holidays = parse_holidays(settings.market_data_holidays)

    def recover(self, underlying: str, now_ist: datetime | None = None) -> None:
        self._advance(underlying, now_ist=now_ist)

    def tick(self, underlying: str, now_ist: datetime | None = None) -> None:
        self._advance(underlying, now_ist=now_ist)

    def tick_all(self, now_ist: datetime | None = None) -> None:
        for underlying in STRADDLE_PULSE_UNDERLYINGS:
            try:
                self.tick(underlying, now_ist=now_ist)
            except Exception:  # noqa: BLE001 - one underlying's failure must not block the other
                logger.exception("straddle_pulse.tick_error underlying=%s", underlying)

    def _advance(self, underlying: str, now_ist: datetime | None = None) -> None:
        from trading.market_data import market_hours as mh

        now_ist = now_ist or mh.now_in_tz(self._tz)
        today = now_ist.date()
        cfg = underlying_config(underlying)
        strike_range = cfg.strike_range(self.settings)

        db = self._session_factory()
        try:
            self.cycles.complete_past_cycles(db, underlying, today)

            expiries = self.master.list_expiries(underlying)
            cycle = self.cycles.get_or_create_active_cycle(db, underlying, expiries, today)
            if cycle is None:
                return  # instrument master not loaded for this underlying yet

            session = self.sessions.get_or_create_today(db, underlying, cycle, today, self._holidays)
            if session is None:
                return  # not a trading day

            session = self.atm.lock_if_due(db, session, self.master, now_ist, self._tz)
            self.oi.snapshot(db, session, self.master, self.cache, strike_range)
        finally:
            db.close()
