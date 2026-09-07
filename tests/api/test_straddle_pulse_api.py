"""Straddle Pulse API -- read endpoints, and cross-underlying isolation
(a NIFTY cycle/session must never resolve or leak SENSEX data)."""
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


def _session(db_session, cycle, trading_date, **kw):
    row = models.DailySession(cycle_id=cycle.id, underlying=cycle.underlying, trading_date=trading_date, **kw)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_underlyings_lists_nifty_and_sensex(client, viewer_auth):
    r = client.get("/api/market/straddle-pulse/underlyings", headers=viewer_auth)
    assert r.status_code == 200
    symbols = {row["symbol"] for row in r.json()}
    assert symbols == {"NIFTY", "SENSEX"}


def test_cycles_scoped_by_underlying(client, viewer_auth, db_session):
    _cycle(db_session, "NIFTY", date(2026, 9, 8))
    _cycle(db_session, "SENSEX", date(2026, 9, 10), exchange="BFO")

    nifty = client.get("/api/market/straddle-pulse/cycles?underlying=NIFTY", headers=viewer_auth).json()
    sensex = client.get("/api/market/straddle-pulse/cycles?underlying=SENSEX", headers=viewer_auth).json()

    assert [c["expiry_date"] for c in nifty] == ["2026-09-08"]
    assert [c["expiry_date"] for c in sensex] == ["2026-09-10"]


def test_unknown_underlying_404(client, viewer_auth):
    assert client.get("/api/market/straddle-pulse/cycles?underlying=BANKNIFTY", headers=viewer_auth).status_code == 404


def test_cycle_sessions_never_cross_underlying(client, viewer_auth, db_session):
    nifty_cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    sensex_cycle = _cycle(db_session, "SENSEX", date(2026, 9, 10), exchange="BFO")
    _session(db_session, nifty_cycle, date(2026, 9, 3), atm_strike=24050, session_status="LOCKED")
    _session(db_session, sensex_cycle, date(2026, 9, 3), atm_strike=81250, session_status="LOCKED")

    nifty_sessions = client.get(
        f"/api/market/straddle-pulse/cycles/{nifty_cycle.id}/sessions", headers=viewer_auth
    ).json()
    assert len(nifty_sessions) == 1
    assert nifty_sessions[0]["underlying"] == "NIFTY"
    assert nifty_sessions[0]["atm_strike"] == 24050


def test_whole_cycle_sessions_in_chronological_order(client, viewer_auth, db_session):
    cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    for d, atm in [(date(2026, 9, 4), 24100), (date(2026, 9, 2), 23900), (date(2026, 9, 3), 24050)]:
        _session(db_session, cycle, d, atm_strike=atm, session_status="LOCKED")

    rows = client.get(f"/api/market/straddle-pulse/cycles/{cycle.id}/sessions", headers=viewer_auth).json()
    assert [r["trading_date"] for r in rows] == ["2026-09-02", "2026-09-03", "2026-09-04"]
    assert [r["atm_strike"] for r in rows] == [23900, 24050, 24100]  # never a blended weekly ATM


def test_session_oi_scoped_and_pcr(client, viewer_auth, db_session):
    cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    session = _session(db_session, cycle, date(2026, 9, 3), atm_strike=24050, session_status="LOCKED")
    db_session.add(models.OISnapshot(
        cycle_id=cycle.id, underlying="NIFTY", expiry_date=date(2026, 9, 8), trading_date=date(2026, 9, 3),
        timestamp=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
        call_oi_total=191400, put_oi_total=81500, call_oi_change=105500, put_oi_change=32700, pcr=0.43,
    ))
    db_session.commit()

    body = client.get(f"/api/market/straddle-pulse/sessions/{session.id}/oi", headers=viewer_auth).json()
    assert len(body["points"]) == 1
    assert body["points"][0]["pcr"] == 0.43


def test_historical_oi_is_read_verbatim_from_storage(client, viewer_auth, db_session):
    """A historical (COMPLETED-cycle) session's OI must come back exactly
    as stored -- the read endpoint never touches a live feed/cache to
    recompute it (it only ever queries the oi_snapshots table)."""
    cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    cycle.status = "COMPLETED"
    db_session.commit()
    session = _session(db_session, cycle, date(2026, 9, 3), atm_strike=24050, session_status="LOCKED")
    db_session.add(models.OISnapshot(
        cycle_id=cycle.id, underlying="NIFTY", expiry_date=date(2026, 9, 8), trading_date=date(2026, 9, 3),
        timestamp=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
        call_oi_total=55555, put_oi_total=22222, call_oi_change=1000, put_oi_change=500, pcr=0.4,
    ))
    db_session.commit()

    body = client.get(f"/api/market/straddle-pulse/sessions/{session.id}/oi", headers=viewer_auth).json()
    assert body["points"][0]["call_oi_total"] == 55555  # exactly the stored value, untouched
    assert body["points"][0]["put_oi_total"] == 22222


def test_session_oi_cross_expiry_isolation(client, viewer_auth, db_session):
    """Two cycles for the same underlying (old COMPLETED + new ACTIVE, as
    across a rollover) must never mix OI rows in the read response."""
    old_cycle = _cycle(db_session, "NIFTY", date(2026, 9, 8))
    old_cycle.status = "COMPLETED"
    new_cycle = _cycle(db_session, "NIFTY", date(2026, 9, 15))
    db_session.commit()

    old_session = _session(db_session, old_cycle, date(2026, 9, 3), atm_strike=24050, session_status="LOCKED")
    new_session = _session(db_session, new_cycle, date(2026, 9, 10), atm_strike=24200, session_status="LOCKED")

    db_session.add_all([
        models.OISnapshot(
            cycle_id=old_cycle.id, underlying="NIFTY", expiry_date=date(2026, 9, 8), trading_date=date(2026, 9, 3),
            timestamp=datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc),
            call_oi_total=100000, put_oi_total=50000, call_oi_change=0, put_oi_change=0, pcr=0.5,
        ),
        models.OISnapshot(
            cycle_id=new_cycle.id, underlying="NIFTY", expiry_date=date(2026, 9, 15), trading_date=date(2026, 9, 10),
            timestamp=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc),
            call_oi_total=300000, put_oi_total=120000, call_oi_change=0, put_oi_change=0, pcr=0.4,
        ),
    ])
    db_session.commit()

    old_body = client.get(f"/api/market/straddle-pulse/sessions/{old_session.id}/oi", headers=viewer_auth).json()
    new_body = client.get(f"/api/market/straddle-pulse/sessions/{new_session.id}/oi", headers=viewer_auth).json()
    assert len(old_body["points"]) == 1 and old_body["points"][0]["call_oi_total"] == 100000
    assert len(new_body["points"]) == 1 and new_body["points"][0]["call_oi_total"] == 300000


def test_session_not_found_404(client, viewer_auth):
    assert client.get("/api/market/straddle-pulse/sessions/999999", headers=viewer_auth).status_code == 404
