"""
Straddle Pulse -- intraday OI build-up + PCR.

Scoped strictly to one (underlying, cycle_id, expiry, trading_date):
totals are summed over the session's locked-ATM +/- strike-range window
(the same window the feed subscribes ticks for -- there is no live OI
for strikes outside it, so this is a real infra limit, not an arbitrary
one), never mixed across underlyings, cycles, or expiries. Only runs
once the day's ATM is locked, since the window is centered on that
immutable ATM.

PCR = put_oi_total / call_oi_total (this project's only PCR definition --
there is no other formula to stay consistent with).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.market_data import option_chain as oc
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.models import DailySession, OISnapshot
from trading.market_data.symbols import make_option_symbol

logger = logging.getLogger("trading.market_data.oi_pcr")


class OIService:
    def snapshot(
        self,
        db: Session,
        session: DailySession,
        master: InstrumentMaster,
        cache: LiveCache,
        strike_range: int,
        now_utc: datetime | None = None,
    ) -> OISnapshot | None:
        if session.session_status != "LOCKED" or session.atm_strike is None:
            return None

        underlying = session.underlying
        expiry = session.cycle.expiry_date
        strikes = master.list_strikes(underlying, expiry)
        window = oc.select_strike_window(strikes, session.atm_strike, strike_range)

        call_total = 0
        put_total = 0
        for strike in window:
            call_total += self._oi(cache, underlying, expiry, strike, "CE")
            put_total += self._oi(cache, underlying, expiry, strike, "PE")

        ts = (now_utc or datetime.now(timezone.utc)).replace(second=0, microsecond=0)

        already = db.execute(
            select(OISnapshot).where(
                OISnapshot.underlying == underlying,
                OISnapshot.expiry_date == expiry,
                OISnapshot.trading_date == session.trading_date,
                OISnapshot.timestamp == ts,
            )
        ).scalar_one_or_none()
        if already is not None:
            return already

        first_of_day = db.execute(
            select(OISnapshot)
            .where(
                OISnapshot.underlying == underlying,
                OISnapshot.expiry_date == expiry,
                OISnapshot.trading_date == session.trading_date,
            )
            .order_by(OISnapshot.timestamp.asc())
            .limit(1)
        ).scalar_one_or_none()
        base_call = first_of_day.call_oi_total if first_of_day else call_total
        base_put = first_of_day.put_oi_total if first_of_day else put_total

        row = OISnapshot(
            cycle_id=session.cycle_id,
            underlying=underlying, expiry_date=expiry, trading_date=session.trading_date, timestamp=ts,
            call_oi_total=call_total, put_oi_total=put_total,
            call_oi_change=call_total - base_call, put_oi_change=put_total - base_put,
            pcr=(put_total / call_total) if call_total > 0 else None,
        )
        db.add(row)
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - concurrent snapshot at the same minute
            db.rollback()
            return db.execute(
                select(OISnapshot).where(
                    OISnapshot.underlying == underlying,
                    OISnapshot.expiry_date == expiry,
                    OISnapshot.trading_date == session.trading_date,
                    OISnapshot.timestamp == ts,
                )
            ).scalar_one_or_none()
        db.refresh(row)
        return row

    @staticmethod
    def _oi(cache: LiveCache, underlying: str, expiry, strike: float, option_type: str) -> int:
        entry = cache.get_option_quote(make_option_symbol(underlying, expiry, strike, option_type))
        if entry is None or entry.quote.oi is None:
            return 0
        return int(entry.quote.oi)
