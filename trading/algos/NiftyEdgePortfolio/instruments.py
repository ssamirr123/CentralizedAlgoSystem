"""
NIFTY option instruments from Angel One's scrip master + trading-day helpers.

Expiry dates come from the exchange master itself (holiday-accurate by
construction), so "expiry day" and "DTE1" never depend on a weekday guess.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import config

_INDEX = {}          # (expiry_iso, strike, opt) -> (symbol, token)
_BY_TOKEN = {}       # token -> (expiry_iso, strike, opt, symbol)
_EXPIRIES: list[date] = []


def load() -> int:
    """Download (or reuse today's cached) scrip master and index NIFTY options."""
    from trading.common.scrip_master import load_scrip_master
    rows = load_scrip_master()
    _INDEX.clear()
    _BY_TOKEN.clear()
    exps = set()
    for r in rows:
        if r.get("exch_seg") != config.OPT_EXCH or r.get("name") != config.INDEX_NAME or r.get("instrumenttype") != "OPTIDX":
            continue
        sym = str(r["symbol"])
        opt = sym[-2:]
        if opt not in ("CE", "PE"):
            continue
        try:
            exp = datetime.strptime(r["expiry"], "%d%b%Y").date()
            strike = int(round(float(r["strike"]) / 100))   # master stores strike x 100
        except (KeyError, ValueError):
            continue
        key = (exp.isoformat(), strike, opt)
        _INDEX[key] = (sym, str(r["token"]))
        _BY_TOKEN[str(r["token"])] = (exp.isoformat(), strike, opt, sym)
        exps.add(exp)
    _EXPIRIES[:] = sorted(exps)
    print(f"[INSTR] {len(_INDEX)} NIFTY options, expiries {[e.isoformat() for e in _EXPIRIES[:4]]} ...")
    return len(_INDEX)


def lookup(expiry: date | str, strike: int, opt: str):
    """(symbol, token) or None."""
    e = expiry.isoformat() if isinstance(expiry, date) else expiry
    return _INDEX.get((e, int(strike), opt))


def by_token(token: str):
    return _BY_TOKEN.get(str(token))


def expiries(on_or_after: date) -> list[date]:
    return [e for e in _EXPIRIES if e >= on_or_after]


def is_trading_day(d: date) -> bool:
    if d.isoformat() in set(getattr(config, "HOLIDAYS", []) or []):
        return False
    from trading.common.market_calendar import is_trading_day as _itd
    return _itd(d)


def next_trading_day(d: date) -> date:
    x = d + timedelta(days=1)
    while not is_trading_day(x):
        x += timedelta(days=1)
    return x


def trading_sessions_until(today: date, expiry: date) -> int:
    n, x = 0, today
    while x < expiry:
        x = next_trading_day(x)
        n += 1
    return n


def previous_trading_days(today: date, n: int) -> list[date]:
    out, x = [], today
    while len(out) < n:
        x -= timedelta(days=1)
        if is_trading_day(x):
            out.append(x)
    return sorted(out)
