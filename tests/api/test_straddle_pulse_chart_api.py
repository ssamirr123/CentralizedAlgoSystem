"""Straddle Pulse API -- GET /sessions/{id}/chart: spot + locked CE/PE
candles for the Daily Straddle chart (Phase 6). Day-scoped, uses the
LOCKED contracts only, never mixes underlyings or trading dates.

This endpoint had a real bug caught during manual browser verification
(spot candles were not scoped by trading_date, so a session's chart
pulled in every day's candles for that symbol) -- these tests are the
automated regression coverage that was missing for it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from trading.database import models


def _cycle(db_session, underlying, expiry, exchange="NFO"):
    row = models.ExpiryCycle(
        underlying=underlying, exchange=exchange, expiry_date=expiry,
        cycle_start_date=date(2026, 9, 2), cycle_end_date=expiry, status="ACTIVE",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _contract(db_session, underlying, expiry, strike, ot, exchange="NFO"):
    symbol = f"{underlying}|{expiry.isoformat()}|{strike:g}|{ot}"
    row = models.OptionContract(
        underlying=underlying, exchange=exchange, provider="icici_breeze",
        provider_token=f"tok-{symbol}", symbol=symbol, expiry=expiry, strike=strike, option_type=ot,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _locked_session(db_session, cycle, trading_date, strike, underlying="NIFTY", spot_0916=None):
    ce = _contract(db_session, underlying, cycle.expiry_date, strike, "CE", exchange=cycle.exchange)
    pe = _contract(db_session, underlying, cycle.expiry_date, strike, "PE", exchange=cycle.exchange)
    row = models.DailySession(
        cycle_id=cycle.id, underlying=underlying, trading_date=trading_date,
        spot_0916=spot_0916 if spot_0916 is not None else 24026.0, atm_strike=strike,
        atm_ce_contract_id=ce.id, atm_pe_contract_id=pe.id, atm_ce_symbol=ce.symbol, atm_pe_symbol=pe.symbol,
        session_status="LOCKED",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row, ce, pe


def _seed_minute_candles(db_session, symbol, exchange, contract_id, trading_date, close_values):
    """Seeds one candle per minute starting 03:45 UTC (=09:15 IST) on
    ``trading_date``, close values as given."""
    for i, close in enumerate(close_values):
        ts = datetime(trading_date.year, trading_date.month, trading_date.day, 3, 45, tzinfo=timezone.utc)
        ts = ts.replace(minute=(45 + i) % 60, hour=3 + (45 + i) // 60)
        if contract_id is None:
            db_session.add(models.MarketCandle(
                timestamp=ts, symbol=symbol, exchange=exchange, interval="1minute",
                open=close, high=close, low=close, close=close, volume=100,
            ))
        else:
            db_session.add(models.OptionCandle(
                timestamp=ts, contract_id=contract_id, open=close, high=close, low=close, close=close,
                volume=50, oi=1000,
            ))
    db_session.commit()


def test_1_nifty_chart_returns_spot_ce_pe(client, viewer_auth, db_session):
    cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    session, ce, pe = _locked_session(db_session, cycle, date(2026, 9, 3), 24050)
    _seed_minute_candles(db_session, "NIFTY", "NSE", None, date(2026, 9, 3), [24000, 24010, 24020])
    _seed_minute_candles(db_session, ce.symbol, "NFO", ce.id, date(2026, 9, 3), [60, 62, 65])
    _seed_minute_candles(db_session, pe.symbol, "NFO", pe.id, date(2026, 9, 3), [140, 138, 135])

    body = client.get(f"/api/market/straddle-pulse/sessions/{session.id}/chart", headers=viewer_auth).json()
    assert body["underlying"] == "NIFTY"
    assert body["atm_strike"] == 24050
    assert [c["close"] for c in body["spot"]] == [24000, 24010, 24020]
    assert [c["close"] for c in body["atm_ce"]] == [60, 62, 65]
    assert [c["close"] for c in body["atm_pe"]] == [140, 138, 135]


def test_2_sensex_chart_independent_of_nifty(client, viewer_auth, db_session):
    nifty_cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    sensex_cycle = _cycle(db_session, "SENSEX", date(2026, 9, 10), exchange="BFO")
    n_session, n_ce, n_pe = _locked_session(db_session, nifty_cycle, date(2026, 9, 3), 24050, underlying="NIFTY")
    s_session, s_ce, s_pe = _locked_session(db_session, sensex_cycle, date(2026, 9, 3), 81250, underlying="SENSEX")

    _seed_minute_candles(db_session, "NIFTY", "NSE", None, date(2026, 9, 3), [24000])
    _seed_minute_candles(db_session, "SENSEX", "BSE", None, date(2026, 9, 3), [81200])
    _seed_minute_candles(db_session, n_ce.symbol, "NFO", n_ce.id, date(2026, 9, 3), [60])
    _seed_minute_candles(db_session, s_ce.symbol, "BFO", s_ce.id, date(2026, 9, 3), [300])

    nifty_body = client.get(f"/api/market/straddle-pulse/sessions/{n_session.id}/chart", headers=viewer_auth).json()
    sensex_body = client.get(f"/api/market/straddle-pulse/sessions/{s_session.id}/chart", headers=viewer_auth).json()
    assert nifty_body["spot"][0]["close"] == 24000
    assert sensex_body["spot"][0]["close"] == 81200
    assert nifty_body["atm_ce"][0]["close"] == 60
    assert sensex_body["atm_ce"][0]["close"] == 300


def test_3_uses_the_locked_atm_contracts_not_a_recomputed_one(client, viewer_auth, db_session):
    """The chart must serve the CE/PE the session actually locked -- not
    whatever the current/live ATM happens to be."""
    cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    session, ce, pe = _locked_session(db_session, cycle, date(2026, 9, 3), 24050)
    # a DIFFERENT contract exists in the DB (e.g. today's live ATM) but is
    # never referenced by this session -- must not appear in the response.
    other_ce = _contract(db_session, "NIFTY", cycle.expiry_date, 24200, "CE")
    _seed_minute_candles(db_session, ce.symbol, "NFO", ce.id, date(2026, 9, 3), [60])
    _seed_minute_candles(db_session, other_ce.symbol, "NFO", other_ce.id, date(2026, 9, 3), [999])

    body = client.get(f"/api/market/straddle-pulse/sessions/{session.id}/chart", headers=viewer_auth).json()
    assert body["atm_ce"] == [] or body["atm_ce"][0]["close"] != 999
    assert body["atm_ce"][0]["close"] == 60


def test_4_spot_overlay_present_and_day_scoped(client, viewer_auth, db_session):
    """Regression test for the day-scoping bug found in manual testing:
    a session's spot series must include ONLY its own trading_date, even
    when the same symbol has candles on other days."""
    cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    session, ce, pe = _locked_session(db_session, cycle, date(2026, 9, 3), 24050)
    _seed_minute_candles(db_session, "NIFTY", "NSE", None, date(2026, 9, 2), [11111])  # a different day
    _seed_minute_candles(db_session, "NIFTY", "NSE", None, date(2026, 9, 3), [24000, 24010])
    _seed_minute_candles(db_session, "NIFTY", "NSE", None, date(2026, 9, 4), [22222])  # another different day

    body = client.get(f"/api/market/straddle-pulse/sessions/{session.id}/chart", headers=viewer_auth).json()
    assert [c["close"] for c in body["spot"]] == [24000, 24010]
    assert 11111 not in [c["close"] for c in body["spot"]]
    assert 22222 not in [c["close"] for c in body["spot"]]


def test_9_historical_daily_session_chart_still_readable(client, viewer_auth, db_session):
    cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    cycle.status = "COMPLETED"
    db_session.commit()
    session, ce, pe = _locked_session(db_session, cycle, date(2026, 9, 3), 24050)
    _seed_minute_candles(db_session, "NIFTY", "NSE", None, date(2026, 9, 3), [24000, 24010])
    _seed_minute_candles(db_session, ce.symbol, "NFO", ce.id, date(2026, 9, 3), [60, 62])

    body = client.get(f"/api/market/straddle-pulse/sessions/{session.id}/chart", headers=viewer_auth).json()
    assert [c["close"] for c in body["spot"]] == [24000, 24010]  # never recalculated, read as stored


def test_10_no_cross_underlying_data_even_with_shared_trading_date(client, viewer_auth, db_session):
    nifty_cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    sensex_cycle = _cycle(db_session, "SENSEX", date(2026, 9, 10), exchange="BFO")
    n_session, n_ce, n_pe = _locked_session(db_session, nifty_cycle, date(2026, 9, 3), 24050, underlying="NIFTY")
    _locked_session(db_session, sensex_cycle, date(2026, 9, 3), 81250, underlying="SENSEX")

    _seed_minute_candles(db_session, "NIFTY", "NSE", None, date(2026, 9, 3), [24000])
    _seed_minute_candles(db_session, "SENSEX", "BSE", None, date(2026, 9, 3), [81200])

    body = client.get(f"/api/market/straddle-pulse/sessions/{n_session.id}/chart", headers=viewer_auth).json()
    assert body["underlying"] == "NIFTY"
    assert [c["close"] for c in body["spot"]] == [24000]
    assert 81200 not in [c["close"] for c in body["spot"]]
