"""Straddle Pulse -- daily ATM lock: computed once from the completed
09:15-09:16 candle, immutable afterwards, restart-safe (idempotent)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from trading.database import models
from trading.market_data.atm_lock import ATMSelectionService
from trading.market_data.daily_session import DailySessionService
from trading.market_data.expiry_cycle import ExpiryCycleService
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.symbols import option_instrument

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2026, 9, 8)
DAY = date(2026, 9, 3)


def _master():
    m = InstrumentMaster()
    m.load(
        [
            option_instrument("NIFTY", EXPIRY, k, ot, lot_size=75, provider="icici_breeze", provider_token=f"t{k}{ot}")
            for k in range(23800, 24201, 50)
            for ot in ("CE", "PE")
        ],
        as_of=date(2026, 9, 1),
    )
    return m


def _session(db_session):
    cycle = ExpiryCycleService().get_or_create_active_cycle(db_session, "NIFTY", [EXPIRY], date(2026, 9, 2))
    return DailySessionService().get_or_create_today(db_session, "NIFTY", cycle, DAY)


def _seed_0916_candle(db_session, close: float):
    minute_start_ist = datetime(2026, 9, 3, 9, 15, tzinfo=IST)
    db_session.add(models.MarketCandle(
        timestamp=minute_start_ist.astimezone(timezone.utc), symbol="NIFTY", exchange="NSE",
        interval="1minute", open=close, high=close, low=close, close=close,
    ))
    db_session.commit()


def test_no_lock_before_0916(db_session):
    session = _session(db_session)
    _seed_0916_candle(db_session, 24026.0)
    now = datetime(2026, 9, 3, 9, 15, 30, tzinfo=IST)
    out = ATMSelectionService().lock_if_due(db_session, session, _master(), now, IST)
    assert out.session_status == "PENDING"


def test_no_lock_without_candle_yet(db_session):
    session = _session(db_session)
    now = datetime(2026, 9, 3, 9, 16, 5, tzinfo=IST)
    out = ATMSelectionService().lock_if_due(db_session, session, _master(), now, IST)
    assert out.session_status == "PENDING"


def test_locks_atm_from_0916_close(db_session):
    session = _session(db_session)
    _seed_0916_candle(db_session, 24026.0)
    now = datetime(2026, 9, 3, 9, 16, 5, tzinfo=IST)
    out = ATMSelectionService().lock_if_due(db_session, session, _master(), now, IST)
    assert out.session_status == "LOCKED"
    assert out.spot_0916 == 24026.0
    assert out.atm_strike == 24050  # nearest listed strike to 24026 (50-wide)
    assert out.atm_ce_symbol == "NIFTY|2026-09-08|24050|CE"
    assert out.atm_pe_symbol == "NIFTY|2026-09-08|24050|PE"


def test_atm_immutable_against_later_price_move(db_session):
    session = _session(db_session)
    _seed_0916_candle(db_session, 24026.0)
    master = _master()
    locked = ATMSelectionService().lock_if_due(
        db_session, session, master, datetime(2026, 9, 3, 9, 16, 5, tzinfo=IST), IST,
    )
    assert locked.atm_strike == 24050

    # a later candle at a very different spot must never change the lock
    db_session.add(models.MarketCandle(
        timestamp=datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc), symbol="NIFTY", exchange="NSE",
        interval="1minute", open=1, high=1, low=1, close=1,
    ))
    still_locked = ATMSelectionService().lock_if_due(
        db_session, locked, master, datetime(2026, 9, 3, 14, 0, tzinfo=IST), IST,
    )
    assert still_locked.atm_strike == 24050
    assert still_locked.session_status == "LOCKED"


def test_restart_recovery_never_recomputes_locked_session(db_session):
    session = _session(db_session)
    _seed_0916_candle(db_session, 24026.0)
    master = _master()
    svc = ATMSelectionService()
    svc.lock_if_due(db_session, session, master, datetime(2026, 9, 3, 9, 16, 5, tzinfo=IST), IST)

    db_session.refresh(session)
    assert session.session_status == "LOCKED"
    before = (session.atm_strike, session.spot_0916)

    # simulate a restart calling recovery again, hours later
    again = svc.lock_if_due(db_session, session, master, datetime(2026, 9, 3, 15, 0, tzinfo=IST), IST)
    assert (again.atm_strike, again.spot_0916) == before
