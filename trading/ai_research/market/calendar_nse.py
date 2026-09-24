"""
NSE (National Stock Exchange of India) trading calendar -- Asia/Kolkata
timezone, real verified trading-holiday data, and session times.

SOURCES REVIEWED (Section 3 -- not guessed, fetched live on 2026-09-22
during this phase's development):
  * Trading holidays: NSE's own public holiday-master API --
    https://www.nseindia.com/api/holiday-master?type=trading
    Capital Market ("CM") category, years 2025 and 2026 -- the SAME
    calendar NSE's own derivatives ("FO") segment publishes (compared
    directly, identical for both years covered here).
  * Session close time: NSE's own live market-status API --
    https://www.nseindia.com/api/marketStatus -- returned
    marketStatusMessage="Normal Market has Closed" at "15:30" on a real
    trading day (2026-09-22), confirming the well-established 15:30 IST
    close.
  * Session open time (09:15 IST) and pre-open window (09:00-09:15 IST)
    are NSE's long-standing, stable, publicly documented normal-market
    timings (unchanged for years) -- not contradicted by anything fetched
    above; no live "session open" event was available to confirm
    independently the way close was, so this one line is the one piece
    here resting on well-established public knowledge rather than a
    fresh API read.

COVERAGE / FAIL-VISIBLE POLICY (Section 9): only 2025 and 2026 holiday
data is embedded (NSE had not yet published 2027's calendar as of this
phase -- confirmed by requesting it live and receiving zero entries).
is_trading_day()/market_open()/market_close() RAISE CalendarCoverageError
for any date outside this range, rather than silently assuming a date is
a trading day just because it isn't a Saturday/Sunday (Section 8's own
explicit warning against "Monday-Friday = trading day"). Refresh by
re-fetching the same URL for a new year and appending to
_TRADING_HOLIDAYS below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE normal-market session (Section 6/7). Pre-open (09:00-09:15 IST) order
# collection is intentionally not modeled separately in this foundation
# phase -- research/backtesting only needs the tradable continuous session.
MARKET_OPEN_TIME = time(9, 15)
MARKET_CLOSE_TIME = time(15, 30)

_COVERED_YEARS = (2025, 2026)


def _d(text: str) -> date:
    return datetime.strptime(text, "%d-%b-%Y").date()


# NSE Capital Market ("CM") trading holidays -- verbatim from the live API
# response, Sr_no order preserved as documentation only.
_TRADING_HOLIDAYS: dict[date, str] = {
    # 2025
    _d("26-Jan-2025"): "Republic Day",
    _d("26-Feb-2025"): "Mahashivratri",
    _d("14-Mar-2025"): "Holi",
    _d("31-Mar-2025"): "Id-Ul-Fitr (Ramadan Eid)",
    _d("06-Apr-2025"): "Shri Ram Navami",
    _d("10-Apr-2025"): "Shri Mahavir Jayanti",
    _d("14-Apr-2025"): "Dr. Baba Saheb Ambedkar Jayanti",
    _d("18-Apr-2025"): "Good Friday",
    _d("01-May-2025"): "Maharashtra Day",
    _d("07-Jun-2025"): "Bakri Id",
    _d("06-Jul-2025"): "Muharram",
    _d("15-Aug-2025"): "Independence Day / Parsi New Year",
    _d("27-Aug-2025"): "Shri Ganesh Chaturthi",
    _d("02-Oct-2025"): "Mahatma Gandhi Jayanti/Dussehra",
    _d("21-Oct-2025"): "Diwali Laxmi Pujan",
    _d("22-Oct-2025"): "Balipratipada",
    _d("05-Nov-2025"): "Prakash Gurpurb Sri Guru Nanak Dev",
    _d("25-Dec-2025"): "Christmas",
    # 2026
    _d("15-Jan-2026"): "Municipal Corporation Election - Maharashtra",
    _d("26-Jan-2026"): "Republic Day",
    _d("15-Feb-2026"): "Mahashivratri",  # falls on a Sunday
    _d("03-Mar-2026"): "Holi",
    _d("21-Mar-2026"): "Id-Ul-Fitr (Ramadan Eid)",
    _d("26-Mar-2026"): "Shri Ram Navami",
    _d("31-Mar-2026"): "Shri Mahavir Jayanti",
    _d("03-Apr-2026"): "Good Friday",
    _d("14-Apr-2026"): "Dr. Baba Saheb Ambedkar Jayanti",
    _d("01-May-2026"): "Maharashtra Day",
    _d("28-May-2026"): "Bakri Id",
    _d("26-Jun-2026"): "Muharram",
    _d("15-Aug-2026"): "Independence Day",
    _d("14-Sep-2026"): "Ganesh Chaturthi",
    _d("02-Oct-2026"): "Mahatma Gandhi Jayanti",
    _d("20-Oct-2026"): "Dussehra",
    _d("08-Nov-2026"): "Diwali Laxmi Pujan",  # falls on a Sunday; NSE marks a Muhurat evening session this date, not modeled here
    _d("10-Nov-2026"): "Diwali-Balipratipada",
    _d("24-Nov-2026"): "Prakash Gurpurb Sri Guru Nanak Dev",
    _d("25-Dec-2026"): "Christmas",
}


class CalendarCoverageError(ValueError):
    """Raised when asked about a date outside the years this module has
    real NSE holiday data for (Section 9: fail visibly, never silently
    treat an unknown date as a trading day)."""


def _check_coverage(d: date) -> None:
    if d.year not in _COVERED_YEARS:
        raise CalendarCoverageError(
            f"no verified NSE trading-calendar data for {d.year} "
            f"(covered years: {_COVERED_YEARS}); refusing to guess whether "
            f"{d.isoformat()} is a trading day"
        )


def is_trading_day(d: date) -> bool:
    """Weekday AND not an NSE trading holiday. Never assumes
    Monday-Friday alone (Section 8)."""
    _check_coverage(d)
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d not in _TRADING_HOLIDAYS


def holiday_name(d: date) -> str | None:
    """The holiday description for d, or None if it's not a listed
    holiday (still check is_trading_day for weekends separately)."""
    _check_coverage(d)
    return _TRADING_HOLIDAYS.get(d)


def previous_trading_day(d: date) -> date:
    cursor = d - timedelta(days=1)
    # Bounded to a generous 30-day lookback -- a real NSE holiday run
    # never comes close to this length; guards against an infinite loop
    # if coverage silently had a gap.
    for _ in range(30):
        if is_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    raise CalendarCoverageError(f"no trading day found within 30 days before {d.isoformat()}")


def next_trading_day(d: date) -> date:
    cursor = d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    raise CalendarCoverageError(f"no trading day found within 30 days after {d.isoformat()}")


def market_open(d: date) -> datetime:
    """IST-aware datetime of the normal-market open on d. Callers should
    check is_trading_day(d) first -- this does not validate that d is a
    trading day, only that it has calendar coverage."""
    _check_coverage(d)
    return datetime.combine(d, MARKET_OPEN_TIME, tzinfo=IST)


def market_close(d: date) -> datetime:
    _check_coverage(d)
    return datetime.combine(d, MARKET_CLOSE_TIME, tzinfo=IST)


@dataclass(frozen=True)
class TradingDateValidation:
    """Section 19: what to tell a caller who requested a non-trading
    research date -- never silently substitute a different date."""

    requested: date
    is_valid: bool
    reason: str | None = None
    previous_trading_day: date | None = None
    next_trading_day: date | None = None


def validate_research_date(d: date) -> TradingDateValidation:
    _check_coverage(d)
    if is_trading_day(d):
        return TradingDateValidation(requested=d, is_valid=True)
    reason = holiday_name(d) or ("weekend" if d.weekday() >= 5 else "non-trading day")
    return TradingDateValidation(
        requested=d, is_valid=False,
        reason=f"{d.isoformat()} is not an NSE trading day ({reason})",
        previous_trading_day=previous_trading_day(d),
        next_trading_day=next_trading_day(d),
    )


def trading_days_in_range(start: date, end: date) -> list[date]:
    """All NSE trading days in [start, end], inclusive. Used by Phase 6
    backtest date-grid generation when market=INDIA (Section 20)."""
    if end < start:
        raise ValueError(f"end ({end}) is before start ({start})")
    for d in (start, end):
        _check_coverage(d)
    days = []
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days
