"""Straddle Pulse -- Phase 8: final end-to-end integration + reliability.

Walks the whole pipeline (expiry -> cycle -> daily session -> 09:16 ATM
lock -> CE/PE -> OI/PCR -> chart/whole-cycle reads) through the real
StraddlePulseEngine + API layer, for both NIFTY and SENSEX, and verifies
rollover, restart, idempotency, cross-underlying isolation, historical
data, and holiday handling all hold together -- not just in isolation
per-service (covered by earlier phases' test files).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from trading.core.config import Settings
from trading.database import models
from trading.database.connection import SessionLocal
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.schemas import OptionQuote
from trading.market_data.straddle_pulse import StraddlePulseEngine
from trading.market_data.symbols import option_instrument

IST = ZoneInfo("Asia/Kolkata")


def _master(pairs: dict[str, list[date]], base: dict[str, int]) -> InstrumentMaster:
    m = InstrumentMaster()
    insts = []
    for underlying, expiries in pairs.items():
        b = base[underlying]
        for exp in expiries:
            for k in range(b - 800, b + 801, 50):
                for ot in ("CE", "PE"):
                    insts.append(option_instrument(
                        underlying, exp, k, ot, lot_size=75,
                        provider="icici_breeze", provider_token=f"t{underlying}{exp}{k}{ot}",
                    ))
    m.load(insts, as_of=date(2026, 9, 1))
    return m


def _engine(master: InstrumentMaster, cache: LiveCache | None = None, holidays: str = "") -> StraddlePulseEngine:
    settings = Settings(market_data_timezone="Asia/Kolkata", market_data_holidays=holidays)
    return StraddlePulseEngine(
        master=master, cache=cache or LiveCache(), session_factory=SessionLocal, settings=settings,
    )


def _seed_0916_candle(db, underlying: str, trading_date: date, close: float, exchange: str):
    minute_ist = datetime(trading_date.year, trading_date.month, trading_date.day, 9, 15, tzinfo=IST)
    db.add(models.MarketCandle(
        timestamp=minute_ist.astimezone(timezone.utc), symbol=underlying, exchange=exchange,
        interval="1minute", open=close, high=close, low=close, close=close,
    ))
    db.commit()


def _seed_oi_quotes(cache: LiveCache, underlying: str, expiry: date, atm: float, call_oi: int, put_oi: int):
    cache.put(OptionQuote.build(underlying=underlying, expiry=expiry, strike=atm, option_type="CE",
                                ltp=100.0, oi=call_oi, provider="icici_breeze"))
    cache.put(OptionQuote.build(underlying=underlying, expiry=expiry, strike=atm, option_type="PE",
                                ltp=90.0, oi=put_oi, provider="icici_breeze"))


def _row(underlying, **kw):
    db = SessionLocal()
    try:
        q = db.query(models.DailySession).filter_by(underlying=underlying, **kw)
        return q.one_or_none()
    finally:
        db.close()


def test_1_2_6_full_pipeline_nifty_and_sensex_independent(db_session):
    """Sections 1, 2, 6: both underlyings walk expiry -> cycle -> session
    -> ATM -> CE/PE -> OI -> PCR independently, and never cross-contaminate."""
    nifty_expiries = [date(2026, 9, 1), date(2026, 9, 8)]
    sensex_expiries = [date(2026, 8, 28), date(2026, 9, 10)]
    master = _master(
        {"NIFTY": nifty_expiries, "SENSEX": sensex_expiries},
        base={"NIFTY": 24000, "SENSEX": 81200},
    )
    cache = LiveCache()
    engine = _engine(master, cache)
    day = date(2026, 9, 3)

    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", day, 24026.0, "NSE")
    _seed_0916_candle(db, "SENSEX", day, 81260.0, "BSE")
    db.close()
    # OI quotes seeded up front: the engine locks ATM and snapshots OI in
    # the same tick, and OI snapshots dedupe by real-clock minute, so a
    # later "second tick" to add OI data would collide with the first
    # (empty) snapshot's timestamp and never be recorded.
    _seed_oi_quotes(cache, "NIFTY", date(2026, 9, 8), 24050, call_oi=100000, put_oi=40000)
    _seed_oi_quotes(cache, "SENSEX", date(2026, 9, 10), 81250, call_oi=300000, put_oi=150000)

    now = datetime(2026, 9, 3, 9, 17, tzinfo=IST)
    engine.tick("NIFTY", now)
    engine.tick("SENSEX", now)

    n_session = _row("NIFTY", trading_date=day)
    s_session = _row("SENSEX", trading_date=day)
    assert n_session.session_status == "LOCKED" and s_session.session_status == "LOCKED"
    assert n_session.atm_strike == 24050  # nearest 50-strike to 24026
    assert s_session.atm_strike == 81250  # nearest 50-strike to 81260
    assert n_session.cycle_id != s_session.cycle_id
    assert n_session.atm_ce_symbol.startswith("NIFTY|")
    assert s_session.atm_ce_symbol.startswith("SENSEX|")

    db = SessionLocal()
    try:
        n_oi = db.query(models.OISnapshot).filter_by(underlying="NIFTY").all()
        s_oi = db.query(models.OISnapshot).filter_by(underlying="SENSEX").all()
        assert len(n_oi) == 1 and n_oi[0].pcr == 0.4
        assert len(s_oi) == 1 and s_oi[0].pcr == 0.5
        assert n_oi[0].cycle_id == n_session.cycle_id
        assert s_oi[0].cycle_id == s_session.cycle_id
    finally:
        db.close()


def test_3_rollover_new_cycle_inherits_nothing(db_session):
    """Section 3: after rollover, the new cycle's session starts
    completely clean -- no ATM, no contracts, no OI carried over."""
    expiries = [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)]
    master = _master({"NIFTY": expiries}, base={"NIFTY": 24000})
    cache = LiveCache()
    engine = _engine(master, cache)

    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", date(2026, 9, 3), 24026.0, "NSE")
    db.close()
    _seed_oi_quotes(cache, "NIFTY", date(2026, 9, 8), 24050, call_oi=100000, put_oi=40000)
    engine.tick("NIFTY", datetime(2026, 9, 3, 9, 17, tzinfo=IST))

    old_session = _row("NIFTY", trading_date=date(2026, 9, 3))
    old_cycle_id = old_session.cycle_id

    # roll over: new trading day, past the old expiry
    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", date(2026, 9, 10), 24310.0, "NSE")
    db.close()
    engine.tick("NIFTY", datetime(2026, 9, 10, 9, 17, tzinfo=IST))

    new_session = _row("NIFTY", trading_date=date(2026, 9, 10))
    assert new_session.cycle_id != old_cycle_id

    db = SessionLocal()
    try:
        old_cycle = db.get(models.ExpiryCycle, old_cycle_id)
        assert old_cycle.status == "COMPLETED"
        new_cycle = db.get(models.ExpiryCycle, new_session.cycle_id)
        assert new_cycle.status == "ACTIVE"
        assert new_cycle.expiry_date == date(2026, 9, 15)

        # nothing carried from the old cycle onto the new one
        assert new_session.atm_strike == 24300  # its own fresh ATM, not 24050
        assert new_session.atm_ce_symbol != old_session.atm_ce_symbol
        new_oi_rows = db.query(models.OISnapshot).filter_by(cycle_id=new_cycle.id).all()
        assert len(new_oi_rows) == 1  # its own snapshot (no live OI seeded for the new window -> zeros)
        assert new_oi_rows[0].call_oi_total == 0 and new_oi_rows[0].put_oi_total == 0  # never the old cycle's 100000/40000
        old_oi_untouched = db.query(models.OISnapshot).filter_by(cycle_id=old_cycle_id).all()
        assert len(old_oi_untouched) == 1 and old_oi_untouched[0].call_oi_total == 100000  # unchanged
    finally:
        db.close()


def test_4_restart_mid_session_restores_everything_no_duplicates(db_session):
    """Section 4: a fresh engine instance (simulating process restart)
    pointed at the same DB/master must restore cycle/session/ATM/OI
    state exactly, with zero duplicate rows."""
    expiries = [date(2026, 9, 1), date(2026, 9, 8)]
    master = _master({"NIFTY": expiries}, base={"NIFTY": 24000})
    cache = LiveCache()
    engine1 = _engine(master, cache)

    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", date(2026, 9, 3), 24026.0, "NSE")
    db.close()
    now = datetime(2026, 9, 3, 9, 17, tzinfo=IST)
    engine1.tick("NIFTY", now)
    _seed_oi_quotes(cache, "NIFTY", date(2026, 9, 8), 24050, call_oi=100000, put_oi=40000)
    engine1.tick("NIFTY", now)

    before = _row("NIFTY", trading_date=date(2026, 9, 3))
    before_snapshot = (before.id, before.cycle_id, before.atm_strike, before.atm_ce_symbol, before.session_status)

    # "restart": a brand-new engine + cache (live cache is in-memory and
    # would be empty on a real restart too), same DB/master.
    engine2 = _engine(master, LiveCache())
    engine2.recover("NIFTY", now)

    db = SessionLocal()
    try:
        assert db.query(models.ExpiryCycle).filter_by(underlying="NIFTY").count() == 1
        assert db.query(models.DailySession).filter_by(underlying="NIFTY").count() == 1
        assert db.query(models.OISnapshot).filter_by(underlying="NIFTY").count() == 1  # not duplicated
        after = db.get(models.DailySession, before.id)
        after_snapshot = (after.id, after.cycle_id, after.atm_strike, after.atm_ce_symbol, after.session_status)
        assert after_snapshot == before_snapshot  # nothing recalculated
    finally:
        db.close()


def test_5_idempotency_across_full_lifecycle_repeated(db_session):
    """Section 5: cycle creation, session creation, ATM selection, OI
    collection, and rollover all repeated many times -- no duplicates."""
    expiries = [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)]
    master = _master({"NIFTY": expiries}, base={"NIFTY": 24000})
    cache = LiveCache()
    engine = _engine(master, cache)

    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", date(2026, 9, 3), 24026.0, "NSE")
    db.close()
    _seed_oi_quotes(cache, "NIFTY", date(2026, 9, 8), 24050, call_oi=100000, put_oi=40000)

    for _ in range(4):
        engine.tick("NIFTY", datetime(2026, 9, 3, 9, 17, tzinfo=IST))

    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", date(2026, 9, 10), 24310.0, "NSE")
    db.close()
    for _ in range(4):
        engine.tick("NIFTY", datetime(2026, 9, 10, 9, 17, tzinfo=IST))  # includes rollover, repeated

    db = SessionLocal()
    try:
        assert db.query(models.ExpiryCycle).filter_by(underlying="NIFTY").count() == 2
        assert db.query(models.DailySession).filter_by(underlying="NIFTY").count() == 2
        # one OI snapshot per trading day, despite 4 repeated ticks each --
        # never one row per tick.
        assert db.query(models.OISnapshot).filter_by(underlying="NIFTY", trading_date=date(2026, 9, 3)).count() == 1
        assert db.query(models.OISnapshot).filter_by(underlying="NIFTY", trading_date=date(2026, 9, 10)).count() == 1
    finally:
        db.close()


def test_7_historical_cycle_data_loaded_verbatim_never_recalculated(client, viewer_auth, db_session):
    """Section 7, exercised through the actual read API (what the
    frontend's historical-cycle selection actually calls)."""
    expiries = [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)]
    master = _master({"NIFTY": expiries}, base={"NIFTY": 24000})
    cache = LiveCache()
    engine = _engine(master, cache)

    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", date(2026, 9, 3), 24026.0, "NSE")
    db.close()
    _seed_oi_quotes(cache, "NIFTY", date(2026, 9, 8), 24050, call_oi=100000, put_oi=40000)
    engine.tick("NIFTY", datetime(2026, 9, 3, 9, 17, tzinfo=IST))

    old_session = _row("NIFTY", trading_date=date(2026, 9, 3))
    old_cycle_id, old_atm, old_ce = old_session.cycle_id, old_session.atm_strike, old_session.atm_ce_symbol

    # roll over -- the live cache changes completely, simulating a brand
    # new day's live prices; the OLD session/OI must still read exactly
    # as originally stored.
    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", date(2026, 9, 10), 24999.0, "NSE")
    db.close()
    cache.clear()
    _seed_oi_quotes(cache, "NIFTY", date(2026, 9, 15), 25000, call_oi=999999, put_oi=999999)
    engine.tick("NIFTY", datetime(2026, 9, 10, 9, 17, tzinfo=IST))

    session_body = client.get(f"/api/market/straddle-pulse/sessions/{old_session.id}", headers=viewer_auth).json()
    oi_body = client.get(f"/api/market/straddle-pulse/sessions/{old_session.id}/oi", headers=viewer_auth).json()
    assert session_body["atm_strike"] == old_atm == 24050
    assert session_body["atm_ce_symbol"] == old_ce
    assert oi_body["points"][0]["call_oi_total"] == 100000  # untouched by the new day's cache


def test_8_holiday_produces_no_fake_data_at_all(db_session):
    """Section 8: no fake session, ATM, or OI for a configured holiday."""
    expiries = [date(2026, 9, 1), date(2026, 9, 8)]
    master = _master({"NIFTY": expiries}, base={"NIFTY": 24000})
    holiday = date(2026, 9, 3)
    engine = _engine(master, LiveCache(), holidays=holiday.isoformat())

    db = SessionLocal()
    _seed_0916_candle(db, "NIFTY", holiday, 24026.0, "NSE")  # even if a candle exists
    db.close()
    engine.tick("NIFTY", datetime(2026, 9, 3, 9, 17, tzinfo=IST))

    assert _row("NIFTY", trading_date=holiday) is None
    db = SessionLocal()
    try:
        assert db.query(models.OISnapshot).filter_by(underlying="NIFTY", trading_date=holiday).count() == 0
    finally:
        db.close()
