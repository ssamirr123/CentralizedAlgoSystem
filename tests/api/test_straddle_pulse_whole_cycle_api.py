"""Straddle Pulse -- Phase 7: Whole Cycle view. The frontend's cycle
selector, whole-cycle table, and whole-cycle chart are all driven by the
existing /cycles, /cycles/{id}/sessions, /sessions/{id}/chart and
/sessions/{id}/oi endpoints reading pure stored data -- these tests
verify that composition end-to-end, including historical cycles."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from trading.database import models


def _cycle(db_session, underlying, expiry, status="ACTIVE", exchange="NFO",
           start=date(2026, 9, 2)):
    row = models.ExpiryCycle(
        underlying=underlying, exchange=exchange, expiry_date=expiry,
        cycle_start_date=start, cycle_end_date=expiry, status=status,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _session(db_session, cycle, trading_date, **kw):
    row = models.DailySession(cycle_id=cycle.id, underlying=cycle.underlying, trading_date=trading_date, **kw)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _oi(db_session, cycle, session, call_oi, put_oi, pcr):
    db_session.add(models.OISnapshot(
        cycle_id=cycle.id, underlying=cycle.underlying, expiry_date=cycle.expiry_date,
        trading_date=session.trading_date, timestamp=datetime(
            session.trading_date.year, session.trading_date.month, session.trading_date.day, 4, 0, tzinfo=timezone.utc
        ),
        call_oi_total=call_oi, put_oi_total=put_oi, call_oi_change=0, put_oi_change=0, pcr=pcr,
    ))
    db_session.commit()


def _weekly_cycle(db_session, underlying="NIFTY", expiry=date(2026, 9, 8), exchange="NFO",
                   start=date(2026, 9, 2)):
    cycle = _cycle(db_session, underlying, expiry, exchange=exchange, start=start)
    days = [
        (start, 23900), (start + timedelta(days=1), 24050), (start + timedelta(days=2), 24100),
        (expiry - timedelta(days=1), 24000), (expiry, 23950),
    ]
    sessions = []
    for d, atm in days:
        s = _session(db_session, cycle, d, atm_strike=atm, session_status="LOCKED")
        _oi(db_session, cycle, s, call_oi=atm * 4, put_oi=atm * 2, pcr=0.5)
        sessions.append(s)
    return cycle, sessions


def test_1_nifty_whole_cycle(client, viewer_auth, db_session):
    cycle, sessions = _weekly_cycle(db_session, "NIFTY")
    rows = client.get(f"/api/market/straddle-pulse/cycles/{cycle.id}/sessions", headers=viewer_auth).json()
    assert [r["trading_date"] for r in rows] == [
        "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08",
    ]


def test_2_sensex_whole_cycle_independent(client, viewer_auth, db_session):
    nifty_cycle, _ = _weekly_cycle(db_session, "NIFTY", date(2026, 9, 8))
    sensex_cycle = _cycle(db_session, "SENSEX", date(2026, 9, 10), exchange="BFO")
    _session(db_session, sensex_cycle, date(2026, 9, 3), atm_strike=81250, session_status="LOCKED")
    _session(db_session, sensex_cycle, date(2026, 9, 10), atm_strike=81500, session_status="LOCKED")

    sensex_rows = client.get(f"/api/market/straddle-pulse/cycles/{sensex_cycle.id}/sessions", headers=viewer_auth).json()
    assert [r["trading_date"] for r in sensex_rows] == ["2026-09-03", "2026-09-10"]
    assert all(r["underlying"] == "SENSEX" for r in sensex_rows)


def test_3_historical_cycle_selection_loads_stored_data(client, viewer_auth, db_session):
    old_cycle, old_sessions = _weekly_cycle(db_session, "NIFTY", date(2026, 9, 8))
    old_cycle.status = "COMPLETED"
    new_cycle, new_sessions = _weekly_cycle(db_session, "NIFTY", date(2026, 9, 15), start=date(2026, 9, 9))
    db_session.commit()

    old_rows = client.get(f"/api/market/straddle-pulse/cycles/{old_cycle.id}/sessions", headers=viewer_auth).json()
    new_rows = client.get(f"/api/market/straddle-pulse/cycles/{new_cycle.id}/sessions", headers=viewer_auth).json()
    assert [r["atm_strike"] for r in old_rows] == [23900, 24050, 24100, 24000, 23950]
    assert old_rows != new_rows
    # selecting the historical cycle never resolves the new cycle's data
    assert {r["trading_date"] for r in old_rows}.isdisjoint({r["trading_date"] for r in new_rows})


def test_4_different_daily_atms_never_blended(client, viewer_auth, db_session):
    cycle, sessions = _weekly_cycle(db_session, "NIFTY")
    rows = client.get(f"/api/market/straddle-pulse/cycles/{cycle.id}/sessions", headers=viewer_auth).json()
    atms = [r["atm_strike"] for r in rows]
    assert atms == [23900, 24050, 24100, 24000, 23950]
    assert len(set(atms)) == len(atms)  # every day genuinely distinct


def test_5_daily_boundaries_are_separate_rows(client, viewer_auth, db_session):
    cycle, sessions = _weekly_cycle(db_session, "NIFTY")
    rows = client.get(f"/api/market/straddle-pulse/cycles/{cycle.id}/sessions", headers=viewer_auth).json()
    assert len(rows) == 5
    assert len({r["id"] for r in rows}) == 5  # 5 distinct session rows, not one merged row


def test_6_holidays_never_appear_as_fake_sessions(client, viewer_auth, db_session):
    """No DailySession row exists for a skipped holiday -- verifying the
    read side never fabricates one when composing the whole-cycle view."""
    cycle, sessions = _weekly_cycle(db_session, "NIFTY")  # 09-05/09-06 (weekend) deliberately absent
    rows = client.get(f"/api/market/straddle-pulse/cycles/{cycle.id}/sessions", headers=viewer_auth).json()
    dates = {r["trading_date"] for r in rows}
    assert "2026-09-05" not in dates and "2026-09-06" not in dates


def test_7_historical_oi_readable_for_every_session_in_old_cycle(client, viewer_auth, db_session):
    old_cycle, old_sessions = _weekly_cycle(db_session, "NIFTY", date(2026, 9, 8))
    old_cycle.status = "COMPLETED"
    db_session.commit()
    for s in old_sessions:
        body = client.get(f"/api/market/straddle-pulse/sessions/{s.id}/oi", headers=viewer_auth).json()
        assert len(body["points"]) == 1
        assert body["points"][0]["call_oi_total"] == s.atm_strike * 4


def test_8_historical_pcr_readable_and_unrecalculated(client, viewer_auth, db_session):
    old_cycle, old_sessions = _weekly_cycle(db_session, "NIFTY", date(2026, 9, 8))
    old_cycle.status = "COMPLETED"
    db_session.commit()
    body = client.get(f"/api/market/straddle-pulse/sessions/{old_sessions[0].id}/oi", headers=viewer_auth).json()
    assert body["points"][0]["pcr"] == 0.5  # exactly what was seeded, never recomputed


def test_9_cross_underlying_isolation_in_whole_cycle(client, viewer_auth, db_session):
    nifty_cycle, nifty_sessions = _weekly_cycle(db_session, "NIFTY", date(2026, 9, 8))
    sensex_cycle = _cycle(db_session, "SENSEX", date(2026, 9, 10), exchange="BFO")
    _session(db_session, sensex_cycle, date(2026, 9, 3), atm_strike=81250, session_status="LOCKED")

    nifty_underlyings = {c["underlying"] for c in client.get(
        "/api/market/straddle-pulse/cycles?underlying=NIFTY", headers=viewer_auth
    ).json()}
    sensex_underlyings = {c["underlying"] for c in client.get(
        "/api/market/straddle-pulse/cycles?underlying=SENSEX", headers=viewer_auth
    ).json()}
    assert nifty_underlyings == {"NIFTY"}
    assert sensex_underlyings == {"SENSEX"}


def test_10_cycle_selector_orders_current_before_older(client, viewer_auth, db_session):
    """The cycle list endpoint (which drives the frontend's Current /
    Previous / Older Cycles selector) must come back ordered so the most
    recent expiry is first."""
    _cycle(db_session, "NIFTY", date(2026, 8, 25), status="COMPLETED")
    _cycle(db_session, "NIFTY", date(2026, 9, 1), status="COMPLETED")
    _cycle(db_session, "NIFTY", date(2026, 9, 8), status="ACTIVE")

    rows = client.get("/api/market/straddle-pulse/cycles?underlying=NIFTY", headers=viewer_auth).json()
    assert [r["expiry_date"] for r in rows] == ["2026-09-08", "2026-09-01", "2026-08-25"]
    assert rows[0]["status"] == "ACTIVE"
    assert all(r["status"] == "COMPLETED" for r in rows[1:])
