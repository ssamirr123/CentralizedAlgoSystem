"""
Phase 6 -- NIFTY option chain.

    get_nifty_option_chain(expiry=None, strike_range=None)

* expiry: None -> current (nearest) expiry; a date -> that expiry; the
  service can also pass the next expiry.
* strike_range: ATM +/- N strikes. Default from NIFTY_OPTION_STRIKE_RANGE.

ATM is computed from the live NIFTY spot; the strike step is derived from
the instrument master (consecutive-strike spacing), never hardcoded.
Contracts with no live quote yet are returned with call/put = null
(fields are never fabricated).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.schemas import OptionChain, OptionChainRow, OptionQuote
from trading.market_data.symbols import make_option_symbol


def nearest_strike(strikes: list[float], spot: float) -> float | None:
    if not strikes or spot is None:
        return None
    return min(strikes, key=lambda s: abs(s - spot))


def strike_step(sorted_strikes: list[float]) -> float | None:
    """Smallest positive gap between consecutive strikes -> the strike
    interval, straight from the listed contracts."""
    diffs = [round(b - a, 6) for a, b in zip(sorted_strikes, sorted_strikes[1:]) if b > a]
    return min(diffs) if diffs else None


def select_strike_window(sorted_strikes: list[float], atm: float, n: int) -> list[float]:
    if atm not in sorted_strikes:
        atm = nearest_strike(sorted_strikes, atm)
    if atm is None:
        return []
    i = sorted_strikes.index(atm)
    lo = max(0, i - n)
    hi = min(len(sorted_strikes), i + n + 1)
    return sorted_strikes[lo:hi]


def resolve_expiry(master: InstrumentMaster, underlying: str, expiry: date | str | None) -> date | None:
    expiries = master.list_expiries(underlying)
    if not expiries:
        return None
    if expiry in (None, "current", "nearest"):
        today = datetime.now(timezone.utc).date()
        upcoming = [e for e in expiries if e >= today]
        return upcoming[0] if upcoming else expiries[-1]
    if expiry == "next":
        today = datetime.now(timezone.utc).date()
        upcoming = [e for e in expiries if e >= today]
        return upcoming[1] if len(upcoming) > 1 else (upcoming[0] if upcoming else expiries[-1])
    if isinstance(expiry, str):
        try:
            expiry = date.fromisoformat(expiry)
        except ValueError:
            return None
    return expiry if expiry in expiries else nearest_expiry(expiries, expiry)


def nearest_expiry(expiries: list[date], target: date) -> date | None:
    return min(expiries, key=lambda e: abs((e - target).days)) if expiries else None


def build_option_chain(
    *,
    underlying: str,
    expiry: date,
    spot: float | None,
    cache: LiveCache,
    master: InstrumentMaster,
    strike_range: int,
    provider_name: str = "icici_breeze",
) -> OptionChain:
    under = underlying.upper()
    all_strikes = master.list_strikes(under, expiry)
    atm = nearest_strike(all_strikes, spot) if (all_strikes and spot is not None) else None
    window = select_strike_window(all_strikes, atm, strike_range) if atm is not None else all_strikes

    rows: list[OptionChainRow] = []
    for strike in window:
        call = _row_quote(cache, under, expiry, strike, "CE")
        put = _row_quote(cache, under, expiry, strike, "PE")
        rows.append(OptionChainRow(strike=strike, call=call, put=put))

    return OptionChain(
        underlying=under, expiry=expiry, spot=spot, atm_strike=atm,
        generated_at=datetime.now(timezone.utc), rows=rows, provider=provider_name,
    )


def _row_quote(cache: LiveCache, underlying: str, expiry: date, strike: float, ot: str) -> OptionQuote | None:
    entry = cache.get_option_quote(make_option_symbol(underlying, expiry, strike, ot))
    if entry is None:
        return None
    q = entry.quote
    return q if isinstance(q, OptionQuote) else None
