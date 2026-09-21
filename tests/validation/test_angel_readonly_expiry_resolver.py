"""Phase 15D.1-B: NIFTY instrument-resolver expiry-selection fix.

Root cause fixed: `_pick_current_nifty_ce_symbol` used to pick the nearest
expiry by calendar date ALONE, with no check for whether that date's own
trading session had already ended -- so once the market closed on an
expiry day, it kept returning that now-unusable contract (Angel's public
scrip master doesn't drop the expired row until it refreshes, typically
the next trading day).

Every test here uses a synthetic, deterministic scrip-master DataFrame
(monkeypatched `pandas.read_json`) and an injected `now` -- no real network
call, no real credentials, no real broker call.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading.validation.angel_readonly import _is_expiry_still_tradable, _pick_current_nifty_ce_symbol

IST = ZoneInfo("Asia/Kolkata")


def _row(expiry: str, symbol: str) -> dict:
    return {"exch_seg": "NFO", "name": "NIFTY", "instrumenttype": "OPTIDX", "expiry": expiry, "symbol": symbol}


def _scrip_master(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _strikes_for(expiry_ddmonyyyy: str, expiry_ddmonyy: str, strikes: list[int]) -> list[dict]:
    return [_row(expiry_ddmonyyyy, f"NIFTY{expiry_ddmonyy}{s}CE") for s in strikes]


# A realistic three-week ladder: a PAST expiry (08SEP2026), TODAY's expiry
# (15SEP2026), and a FUTURE expiry (22SEP2026), each with a few strikes
# around 23000-23200.
_THREE_WEEK_LADDER = (
    _strikes_for("08SEP2026", "08SEP26", [22900, 23000, 23100])
    + _strikes_for("15SEP2026", "15SEP26", [23000, 23100, 23200])
    + _strikes_for("22SEP2026", "22SEP26", [23000, 23100, 23200])
)


@pytest.fixture(autouse=True)
def _mock_scrip_master(monkeypatch):
    """Default fixture -- individual tests override via monkeypatch again
    if they need a different ladder."""
    monkeypatch.setattr(pd, "read_json", lambda url: _scrip_master(_THREE_WEEK_LADDER))
    yield


# --------------------------------------------------------------------------- #
# 1. Expired expiry is rejected
# --------------------------------------------------------------------------- #
def test_expired_expiry_is_rejected():
    """now is AFTER both the past (08SEP) and today (15SEP) expiries have
    elapsed -- only 22SEP remains eligible."""
    now = datetime(2026, 9, 16, 10, 0, tzinfo=IST)  # the day after 15SEP
    symbol, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry == "2026-09-22"
    assert "22SEP26" in symbol


# --------------------------------------------------------------------------- #
# 2. Same-day invalid expiry rejected once the session has closed
# --------------------------------------------------------------------------- #
def test_same_day_expiry_rejected_after_session_close():
    now = datetime(2026, 9, 15, 16, 0, tzinfo=IST)  # 15SEP, 16:00 IST -- market closed
    symbol, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry == "2026-09-22"  # 15SEP rejected despite being "today"


def test_same_day_expiry_accepted_before_session_close():
    now = datetime(2026, 9, 15, 10, 0, tzinfo=IST)  # 15SEP, 10:00 IST -- market open
    symbol, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry == "2026-09-15"
    assert "15SEP26" in symbol


# --------------------------------------------------------------------------- #
# 3. Next valid weekly expiry is selected (nearest is expired)
# --------------------------------------------------------------------------- #
def test_next_valid_weekly_expiry_selected_when_nearest_is_expired(monkeypatch):
    ladder = _strikes_for("08SEP2026", "08SEP26", [23100]) + _strikes_for("22SEP2026", "22SEP26", [23100])
    monkeypatch.setattr(pd, "read_json", lambda url: _scrip_master(ladder))
    now = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    symbol, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry == "2026-09-22"


# --------------------------------------------------------------------------- #
# 4. Malformed expiry is rejected (skipped, not crashed on)
# --------------------------------------------------------------------------- #
def test_malformed_expiry_is_skipped(monkeypatch):
    ladder = [_row("NOT-A-DATE", "NIFTYNOT-A-DATE23100CE")] + _strikes_for("22SEP2026", "22SEP26", [23100])
    monkeypatch.setattr(pd, "read_json", lambda url: _scrip_master(ladder))
    now = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    symbol, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry == "2026-09-22"


# --------------------------------------------------------------------------- #
# 5. Multiple valid expiries -> nearest valid one is selected
# --------------------------------------------------------------------------- #
def test_multiple_valid_expiries_selects_nearest():
    now = datetime(2026, 9, 10, 10, 0, tzinfo=IST)  # before all three expiries
    symbol, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry == "2026-09-15"  # nearest of the three, still in the future relative to now


# --------------------------------------------------------------------------- #
# 6. Weekend boundary: the calendar date rolling over a closed weekend must
# not cause an expired weekday contract to be treated as valid
# --------------------------------------------------------------------------- #
def test_weekend_boundary_does_not_resurrect_an_expired_contract():
    # 15SEP2026 (a Tuesday, per this project's own dated data) has already
    # elapsed; "now" is the following Saturday -- 15SEP must still be
    # rejected (it is well in the past), not accidentally accepted because
    # the market happens to be closed for the weekend right now too.
    now = datetime(2026, 9, 19, 12, 0, tzinfo=IST)  # Saturday, after 15SEP has fully elapsed
    symbol, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry == "2026-09-22"  # the next (future) expiry, not the elapsed one


# --------------------------------------------------------------------------- #
# 7. An expired instrument can never reach the returned result
# --------------------------------------------------------------------------- #
def test_expired_instrument_never_reaches_the_return_value():
    now = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    _, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry != "2026-09-08"
    assert expiry != "2026-09-15"


# --------------------------------------------------------------------------- #
# 8. No valid candidate at all -> fails closed with a clear error, not a
# silent fallback to an expired one
# --------------------------------------------------------------------------- #
def test_no_valid_expiry_raises_value_error(monkeypatch):
    ladder = _strikes_for("08SEP2026", "08SEP26", [23100])  # only a past expiry exists
    monkeypatch.setattr(pd, "read_json", lambda url: _scrip_master(ladder))
    now = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
    with pytest.raises(ValueError):
        _pick_current_nifty_ce_symbol(23100.0, now=now)


# --------------------------------------------------------------------------- #
# 9. Symbol/expiry mismatch (an expiry with no CE rows at all) fails closed
# by skipping to the next candidate, not by crashing or returning garbage
# --------------------------------------------------------------------------- #
def test_expiry_with_no_ce_rows_is_skipped(monkeypatch):
    ladder = [_row("15SEP2026", "NIFTY15SEP2623100PE")] + _strikes_for("22SEP2026", "22SEP26", [23100])
    monkeypatch.setattr(pd, "read_json", lambda url: _scrip_master(ladder))
    now = datetime(2026, 9, 10, 10, 0, tzinfo=IST)  # 15SEP would otherwise be nearest+valid
    symbol, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    assert expiry == "2026-09-22"  # 15SEP had no CE row -- skipped


# --------------------------------------------------------------------------- #
# 10. The resolver never returns an expired instrument, across a sweep of
# "now" values spanning before/at/after the boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 9, 15, 9, 0, tzinfo=IST),
        datetime(2026, 9, 15, 15, 29, tzinfo=IST),
        datetime(2026, 9, 15, 15, 31, tzinfo=IST),
        datetime(2026, 9, 15, 23, 59, tzinfo=IST),
        datetime(2026, 9, 16, 0, 0, 1, tzinfo=IST),
        datetime(2026, 9, 20, 0, 0, tzinfo=IST),
    ],
)
def test_resolver_never_returns_an_expired_instrument(now):
    _, expiry = _pick_current_nifty_ce_symbol(23100.0, now=now)
    resolved_date = datetime.fromisoformat(expiry).date()
    local_now = now.astimezone(IST)
    assert _is_expiry_still_tradable(resolved_date, local_now) is True


# --------------------------------------------------------------------------- #
# ATM strike selection is preserved (not touched by the expiry fix)
# --------------------------------------------------------------------------- #
def test_atm_strike_selection_still_picks_the_closest_strike():
    now = datetime(2026, 9, 10, 10, 0, tzinfo=IST)
    symbol, _ = _pick_current_nifty_ce_symbol(23105.0, now=now)  # closest to 23100 given round-to-50
    assert symbol == "NIFTY15SEP2623100CE"
