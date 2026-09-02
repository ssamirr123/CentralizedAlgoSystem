"""Phase 7 -- live cache + stale detection."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading.market_data.cache import LiveCache
from trading.market_data.schemas import IndexQuote, OptionQuote

T0 = datetime(2026, 9, 7, 9, 20, tzinfo=timezone.utc)


def _iq(sym, ltp, when=T0):
    return IndexQuote(symbol=sym, ltp=ltp, prev_close=ltp - 1, received_at=when, provider="icici_breeze")


def test_put_and_get_index():
    c = LiveCache(stale_seconds=10)
    c.put(_iq("NIFTY", 25000))
    e = c.get_latest_quote("NIFTY")
    assert e is not None and e.quote.ltp == 25000
    assert c.get_option_quote("NIFTY") is None  # not an option


def test_stale_detection():
    c = LiveCache(stale_seconds=10)
    c.put(_iq("NIFTY", 25000, T0))
    assert c.is_stale("NIFTY", now=T0 + timedelta(seconds=5)) is False
    assert c.is_stale("NIFTY", now=T0 + timedelta(seconds=11)) is True
    assert c.get_latest_quote("NIFTY").status(10, now=T0 + timedelta(seconds=11)) == "stale"
    assert c.is_stale("BANKNIFTY") is True  # never seen


def test_symbols_live_counts_only_fresh():
    c = LiveCache(stale_seconds=10)
    c.put(_iq("NIFTY", 25000, T0))
    c.put(_iq("BANKNIFTY", 52000, T0 - timedelta(seconds=30)))
    assert c.symbols_live(now=T0) == 1


def test_option_quote_kept_separately():
    from datetime import date

    from trading.market_data.symbols import make_option_symbol

    c = LiveCache(stale_seconds=10)
    expiry = date(2026, 9, 10)
    q = OptionQuote(
        symbol=make_option_symbol("NIFTY", expiry, 25000, "CE"),
        underlying="NIFTY", expiry=expiry, strike=25000.0, option_type="CE",
        ltp=120.0, received_at=T0, provider="icici_breeze",
    )
    c.put(q)
    assert c.get_option_quote(q.symbol) is not None
    assert c.get_latest_quote(q.symbol) is not None
    assert c.last_tick_at == T0


def test_clear():
    c = LiveCache()
    c.put(_iq("NIFTY", 1))
    c.clear()
    assert c.get_latest_quote("NIFTY") is None and c.last_tick_at is None
