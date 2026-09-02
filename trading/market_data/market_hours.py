"""
Phase 4 / Phase 15 -- market-time logic.

All scheduling decisions use timezone-aware ``Asia/Kolkata`` timestamps.
``datetime.now()`` (naive / EC2-local) is NEVER used for a scheduling
decision; the only wall-clock read is ``now_in_tz`` and every other
function takes ``now`` as an argument so it is fully testable (Phase 22).

Holidays: a set of ``date`` is threaded through; empty means "weekday =
trading day". Phase 15 can swap the empty default for a real NSE/BSE
calendar without changing callers.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_DEFAULT_TZ = "Asia/Kolkata"


def market_tz(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or _DEFAULT_TZ)


def now_in_tz(tz: ZoneInfo) -> datetime:
    """The single wall-clock read for the scheduler."""
    return datetime.now(tz)


def parse_hhmm(value: str) -> time:
    """'09:10' -> time(9, 10). Raises ValueError on bad input."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {value!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"HH:MM out of range: {value!r}")
    return time(h, m)


def parse_holidays(value: str | None) -> set[date]:
    out: set[date] = set()
    for token in (value or "").replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(date.fromisoformat(token))
        except ValueError:
            continue
    return out


def is_trading_day(d: date, holidays: set[date] | None = None) -> bool:
    """Mon-Fri and not an exchange holiday. NOT just weekday==<5."""
    if d.weekday() >= 5:  # Sat/Sun
        return False
    return d not in (holidays or set())


def session_window(
    day: date, start: time, stop: time, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """tz-aware (start, stop) datetimes for ``day``."""
    return (
        datetime.combine(day, start, tzinfo=tz),
        datetime.combine(day, stop, tzinfo=tz),
    )


def market_is_open(
    now: datetime,
    *,
    start: time,
    stop: time,
    tz: ZoneInfo,
    holidays: set[date] | None = None,
) -> bool:
    """True when ``now`` is on a trading day and start <= now < stop."""
    local = now.astimezone(tz)
    if not is_trading_day(local.date(), holidays):
        return False
    open_dt, close_dt = session_window(local.date(), start, stop, tz)
    return open_dt <= local < close_dt


def next_market_start(
    now: datetime,
    *,
    start: time,
    stop: time,
    tz: ZoneInfo,
    holidays: set[date] | None = None,
    max_lookahead_days: int = 14,
) -> datetime | None:
    """The next tz-aware session-start >= now (today if still before start
    on a trading day, else the next trading day). None if none within
    ``max_lookahead_days`` (e.g. a long holiday list)."""
    local = now.astimezone(tz)
    for offset in range(0, max_lookahead_days + 1):
        day = local.date() + timedelta(days=offset)
        if not is_trading_day(day, holidays):
            continue
        open_dt, _ = session_window(day, start, stop, tz)
        if open_dt >= local:
            return open_dt
    return None


def market_close(
    now: datetime,
    *,
    start: time,
    stop: time,
    tz: ZoneInfo,
    holidays: set[date] | None = None,
) -> datetime | None:
    """Today's tz-aware session-close, or None if today is not a trading day."""
    local = now.astimezone(tz)
    if not is_trading_day(local.date(), holidays):
        return None
    _, close_dt = session_window(local.date(), start, stop, tz)
    return close_dt
