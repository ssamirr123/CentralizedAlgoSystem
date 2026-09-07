"""
Straddle Pulse -- daily ATM lock.

Rule (spec section 8/10): at 09:16 IST, take the CLOSE of the completed
09:15-09:16 one-minute candle, compute ATM, and lock it for the rest of
the day. Once ``session_status == "LOCKED"`` this never recomputes,
regardless of later price movement (immutability) or how many times this
is called (idempotent -- restart-safe, spec section 29/30).

Generic over ``underlying``: the candle is looked up by the underlying's
own internal symbol, and ATM/strike-window derivation reuses
``option_chain`` exactly as the option-chain feature does today.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.market_data import option_chain as oc
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.models import DailySession, MarketCandle, OptionContract
from trading.market_data.symbols import make_option_symbol

logger = logging.getLogger("trading.market_data.atm_lock")

_LOCK_TIME = time(9, 16)
_CANDLE_MINUTE = time(9, 15)


class ATMSelectionService:
    def lock_if_due(
        self,
        db: Session,
        session: DailySession,
        master: InstrumentMaster,
        now_ist: datetime,
        tz: ZoneInfo,
    ) -> DailySession:
        if session.session_status == "LOCKED":
            return session

        lock_at = datetime.combine(session.trading_date, _LOCK_TIME, tzinfo=tz)
        if now_ist < lock_at:
            return session

        candle_minute = datetime.combine(session.trading_date, _CANDLE_MINUTE, tzinfo=tz)
        candle_minute_utc = candle_minute.astimezone(timezone.utc)
        candle = db.execute(
            select(MarketCandle).where(
                MarketCandle.symbol == session.underlying,
                MarketCandle.timestamp == candle_minute_utc,
            )
        ).scalar_one_or_none()
        if candle is None:
            logger.debug(
                "straddle_pulse.atm_lock_pending underlying=%s date=%s (09:15 candle not persisted yet)",
                session.underlying, session.trading_date,
            )
            return session

        spot = candle.close
        expiry = session.cycle.expiry_date
        strikes = master.list_strikes(session.underlying, expiry)
        atm = oc.nearest_strike(strikes, spot)
        if atm is None:
            return session

        ce = master.resolve(session.underlying, expiry, atm, "CE")
        pe = master.resolve(session.underlying, expiry, atm, "PE")

        ce_symbol = ce.internal_symbol if ce else make_option_symbol(session.underlying, expiry, atm, "CE")
        pe_symbol = pe.internal_symbol if pe else make_option_symbol(session.underlying, expiry, atm, "PE")

        session.spot_0916 = spot
        session.atm_strike = atm
        session.atm_ce_symbol = ce_symbol
        session.atm_pe_symbol = pe_symbol
        session.atm_ce_contract_id = self._contract_id(db, ce_symbol)
        session.atm_pe_contract_id = self._contract_id(db, pe_symbol)
        session.session_status = "LOCKED"
        db.commit()
        db.refresh(session)
        logger.info(
            "straddle_pulse.atm_locked underlying=%s date=%s spot=%.2f atm=%.2f",
            session.underlying, session.trading_date, spot, atm,
        )
        return session

    @staticmethod
    def _contract_id(db: Session, symbol: str) -> int | None:
        row = db.execute(
            select(OptionContract.id).where(OptionContract.symbol == symbol)
        ).scalar_one_or_none()
        return row
