"""
Unit tests for trading/algos/NiftyEdgePortfolio (no network, no broker, no data files).

The algo uses flat module names (config, feed, strategies, ...) like the other
AngelOne algos, so every test runs with a clean module cache pointed at this
algo's folder.
"""
from __future__ import annotations

import importlib
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

ALGO = Path(__file__).resolve().parents[2] / "trading" / "algos" / "NiftyEdgePortfolio"
_MODS = ("config", "context", "feed", "instruments", "engine", "live_context", "monitor", "connectapi", "log_setup",
         "strategies", "state", "broker", "risk", "replay")


def _purge():
    for name in list(sys.modules):
        if name.split(".")[0] in _MODS:
            del sys.modules[name]


@pytest.fixture()
def nep(monkeypatch):
    monkeypatch.delenv("NEP_LIVE", raising=False)
    monkeypatch.setenv("BOT_DRY_RUN", "false")      # another algo's live switch must NOT affect this one
    _purge()
    sys.path.insert(0, str(ALGO))
    try:
        yield
    finally:
        sys.path.remove(str(ALGO))
        _purge()


def _batch_supertrend(h, l, c, n=10, mult=3.0):
    """Verbatim copy of the research backtest function (p12_investoruday.supertrend)."""
    N = len(c); tr = np.empty(N); tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    atr = np.empty(N); atr[:n] = tr[:n].mean()
    for i in range(n, N): atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    hl2 = (h + l) / 2; ub = hl2 + mult * atr; lb = hl2 - mult * atr; fu = ub.copy(); fl = lb.copy(); dr = np.ones(N, int)
    for i in range(1, N):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if dr[i - 1] == -1 and c[i] > fu[i - 1]: dr[i] = 1
        elif dr[i - 1] == 1 and c[i] < fl[i - 1]: dr[i] = -1
        else: dr[i] = dr[i - 1]
    return dr


def test_dry_run_is_the_default(nep):
    config = importlib.import_module("config")
    assert config.DRY_RUN is True
    assert config.PRODUCT_TYPE == "CARRYFORWARD"
    assert config.MANUAL_GUARD["auto_exit"] is False


def test_supertrend_incremental_equals_backtest(nep):
    st10 = importlib.import_module("strategies.st10")
    rng = np.random.default_rng(1)
    c = 20000 + np.cumsum(rng.normal(0, 15, 3000))
    h = c + rng.uniform(0, 20, 3000)
    l = c - rng.uniform(0, 20, 3000)
    want = _batch_supertrend(h, l, c)
    s = st10.Supertrend(10, 3.0)
    got = []
    for i in range(3000):
        s.update(h[i], l[i], c[i])
        if i >= 9:
            got.append(s.dir)
        if i == 1500:      # survives a save/restore round-trip mid-stream
            s = st10.Supertrend.from_dict(s.to_dict())
    assert got == list(want[9:])
    assert (np.diff(want) != 0).sum() > 50      # the test series actually flips


def test_charges_match_backtest_model(nep):
    ctx = importlib.import_module("context")
    # backtest chg(p, q, 'S', d) * q for 60 x 65 on 2026-06-02
    p, q = 60.0, 65
    prem = p * q; ex = prem * 0.0003503; sebi = prem * 1e-6
    want = 20 + ex + sebi + prem * 0.0015 + 0.18 * (20 + ex + sebi)
    assert ctx.charges(p, q, "SELL", date(2026, 6, 2)) == pytest.approx(want)


class FakeCtx:
    """Minimal in-memory context for rule tests."""

    def __init__(self, today, expiries, spot, chain):
        self.today, self._exp, self._spot, self._chain = today, expiries, spot, chain
        self.minute = 0
        self.opened, self.closed, self.st = [], [], {}

    def spot(self): return self._spot
    def option(self, e, k, opt): return self._chain.get((k, opt))
    def chain(self, e, opt, strikes): return {k: v for (k, o), v in self._chain.items() if o == opt and k in strikes}
    def liquid(self, e, k, opt): return True
    def expiries(self): return self._exp
    def nearest_expiry(self): return self._exp[0]
    def trading_sessions_until(self, e): return (e - self.today).days
    def sstate(self, s): return self.st.setdefault(s, {})
    def save(self): pass
    def can_enter(self, s, lots): return True
    def log(self, m): pass
    def open_legs(self, s): return [l for l in self.opened if l.status == "OPEN"]

    def open_leg(self, leg, reason):
        from strategies.base import Strategy
        leg.entry_price = self.option(None, leg.strike, leg.opt)
        leg.entry_dt = f"{self.today} 09:20"
        Strategy.set_stops(leg)
        self.opened.append(leg)
        return leg

    def close_leg(self, leg, reason):
        leg.status = "CLOSED"
        leg.exit_reason = reason
        self.closed.append((leg.opt, reason, self.minute))
        return True


def test_strangle60_picks_nearest_60_and_stops_on_close(nep):
    config = importlib.import_module("config")
    S = importlib.import_module("strategies.strangle60").Strangle60
    d = date(2026, 6, 2)
    chain = {(25000 + 50 * i, "CE"): max(0.5, 150 - 25 * i) for i in range(0, 12)}      # 150,125,100,75,50,...
    chain.update({(25000 - 50 * i, "PE"): max(0.5, 140 - 22 * i) for i in range(0, 12)})  # 140,118,96,74,52,...
    ctx = FakeCtx(d, [d], 25010.0, chain)
    s = S(config.STRANGLE60, lots=1, tag="T", lot_size=65)
    for m in range(0, 6):
        ctx.minute = m
        s.on_minute(ctx)
    picked = ctx.st["strangle60"]["picked"]
    assert picked == {"CE": 25200, "PE": 24800}        # CE: 50 (diff 10) beats 75 (diff 15); PE: 52 beats 74
    assert {l.opt: l.sl for l in ctx.opened} == {"CE": 100.0, "PE": 104.0}
    ctx.minute = 6
    chain[(25200, "CE")] = 99.95                       # just below 2x entry: no stop
    s.on_minute(ctx)
    assert ctx.closed == []
    ctx.minute = 7
    chain[(25200, "CE")] = 100.0                       # close reaches 2x entry
    s.on_minute(ctx)
    assert ("CE", "SL", 7) in ctx.closed
    ctx.minute = 360
    s.on_minute(ctx)
    assert ("PE", "TIME_1515", 360) in ctx.closed


def test_strangle60_skips_non_expiry_and_far_premiums(nep):
    config = importlib.import_module("config")
    S = importlib.import_module("strategies.strangle60").Strangle60
    d = date(2026, 6, 1)
    ctx = FakeCtx(d, [date(2026, 6, 2)], 25000.0, {(25000, "CE"): 60.0, (25000, "PE"): 60.0})
    s = S(config.STRANGLE60, lots=1, tag="T", lot_size=65)
    for m in range(0, 10):
        ctx.minute = m
        s.on_minute(ctx)
    assert ctx.opened == []                             # not expiry day
    ctx2 = FakeCtx(d, [d], 25000.0, {(25000, "CE"): 100.0, (25000, "PE"): 60.0})
    for m in range(0, 10):
        ctx2.minute = m
        s.on_minute(ctx2)
    assert ctx2.opened == []                            # CE best premium 100 is > Rs18 from 60


def test_state_round_trip_keeps_overnight_legs(nep, tmp_path, monkeypatch):
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    store = importlib.import_module("state.store")
    Leg = importlib.import_module("context").Leg
    st = store.load()
    leg = Leg(strategy="otm4_1day", tag="NEP_OTM4", expiry="2026-06-02", strike=25200, opt="CE", qty=65, entry_price=40.0, sl=60.0)
    store.put_leg(st, leg)
    st["sstates"]["st10"] = {"prev_dir": -1}
    store.save(st)
    st2 = store.load()
    assert [l.id for l in store.legs(st2)] == [leg.id]
    assert st2["sstates"]["st10"]["prev_dir"] == -1


def test_paper_fill_uses_ltp_minus_slippage(nep, monkeypatch):
    importlib.import_module("config")
    feed = importlib.import_module("feed")
    orders = importlib.import_module("broker.orders")
    monkeypatch.setattr(feed, "get_ltp", lambda token, exchange=None, fallback_rest=True: 61.0)
    assert orders.execute("NIFTY02JUN2625150CE", "123", 65, "SELL", "NEP_S60") == (65, 60.5)
    assert orders.execute("NIFTY02JUN2625150CE", "123", 65, "BUY", "NEP_S60") == (65, 61.5)


def test_holiday_guard_needs_the_index_to_move(nep):
    config = importlib.import_module("config")
    feed = importlib.import_module("feed")
    config.objconn = None
    tick = lambda px: feed._on_data(None, {"token": config.INDEX_TOKEN, "last_traded_price": int(px * 100)})
    for _ in range(50):                 # holiday: the feed only repeats yesterday's close
        tick(24500.0)
    assert feed.market_live() is False
    for px in (24501.0, 24503.5, 24499.0):   # a real session: the index moves
        tick(px)
    assert feed.market_live() is True


class _RestOnly:
    def __init__(self, prices):
        self.prices = list(prices)

    def ltpData(self, exch, sym, tok):
        return {"data": {"ltp": self.prices.pop(0)}}


def test_holiday_guard_rest_fallback_when_websocket_is_down(nep):
    config = importlib.import_module("config")
    feed = importlib.import_module("feed")
    config.objconn = _RestOnly([24500.0, 24500.0, 24500.0])
    assert feed.market_live() is False and feed.market_live() is False and feed.market_live() is False
    config.objconn = _RestOnly([24510.0])
    assert feed.market_live() is True          # 24500 -> 24510 across two samples


class StbtCtx(FakeCtx):
    """FakeCtx + 1-min spot bars and entry_expiry for the STBT strategy."""

    def __init__(self, today, expiries, chain, bars):
        super().__init__(today, expiries, None, chain)
        self.bars = bars                     # {minute: (o, h, l, c)}

    def spot_bars(self, n):
        return [(j, *b) for j, b in sorted(self.bars.items()) if j <= self.minute]

    def entry_expiry(self):
        return self._exp[1] if self._exp[0] == self.today and len(self._exp) > 1 else self._exp[0]


def test_stbt_breakout_sells_itm_against_it_and_uses_spot_stops(nep):
    config = importlib.import_module("config")
    S = importlib.import_module("strategies.stbt").Stbt
    d, nd = date(2026, 6, 1), date(2026, 6, 2)
    bars = {j: (25000.0, 25040.0, 24960.0, 25000.0) for j in range(120)}         # range 24960-25040
    bars.update({j: (25000.0, 25030.0, 24990.0, 25010.0) for j in range(120, 150)})
    bars[150] = (25030.0, 25060.0, 25030.0, 25052.0)                               # close > high -> UP
    chain = {(25250, "PE"): 230.0, (24850, "CE"): 240.0}
    ctx = StbtCtx(d, [date(2026, 6, 4)], chain, bars)
    s = S(config.STBT, lots=1, tag="T", lot_size=65)
    for m in range(0, 151):
        ctx.minute = m
        s.on_minute(ctx)
    assert [(l.strike, l.opt) for l in ctx.opened] == [(25250, "PE")]           # ATM 25050 + 200, sold PE
    leg = ctx.opened[0]
    assert leg.meta["dir"] == "UP" and leg.meta["spot_sl"] == round(25052 * 0.995, 2)
    assert leg.sl is None and leg.disaster_sl == 460.0                           # 2x safety stop only
    for m in range(151, 375):                                                    # no second trade, no stop
        ctx.minute = m
        ctx.bars[m] = (25050.0, 25060.0, 25040.0, 25050.0)
        s.on_minute(ctx)
    assert len(ctx.opened) == 1 and ctx.closed == []
    # next session: a low touching the spot stop at 09:22 -> exit
    ctx.today, ctx.bars = nd, {j: (25000.0, 25010.0, 24990.0, 25000.0) for j in range(7)}
    ctx.bars[7] = (24950.0, 24960.0, 24920.0, 24930.0)
    for m in range(0, 8):
        ctx.minute = m
        s.on_minute(ctx)
    assert ctx.closed == [("PE", "SL_SPOT_NEXTDAY", 7)]


def test_stbt_time_exit_and_late_breakout_skip(nep):
    config = importlib.import_module("config")
    S = importlib.import_module("strategies.stbt").Stbt
    d, nd = date(2026, 6, 1), date(2026, 6, 2)
    bars = {j: (25000.0, 25040.0, 24960.0, 25000.0) for j in range(120)}
    bars[120] = (24990.0, 24990.0, 24940.0, 24950.0)                              # close < low -> DOWN
    ctx = StbtCtx(d, [date(2026, 6, 4)], {(24750, "CE"): 300.0}, bars)
    s = S(config.STBT, lots=1, tag="T", lot_size=65)
    for m in range(0, 121):
        ctx.minute = m
        s.on_minute(ctx)
    assert [(l.strike, l.opt) for l in ctx.opened] == [(24750, "CE")]          # ATM 24950 - 200, sold CE
    ctx.today, ctx.bars = nd, {j: (24900.0, 24910.0, 24890.0, 24900.0) for j in range(25)}
    for m in range(0, 25):
        ctx.minute = m
        s.on_minute(ctx)
    assert ctx.closed == [("CE", "TIME_0935", 20)]
    # restart long after the breakout: the strategy must not enter late
    ctx2 = StbtCtx(d, [date(2026, 6, 4)], {(24750, "CE"): 300.0}, dict(bars))
    s2 = S(config.STBT, lots=1, tag="T", lot_size=65)
    ctx2.minute = 140
    s2.on_minute(ctx2)
    assert ctx2.opened == []


def test_feed_builds_1min_index_bars_from_ticks(nep):
    from datetime import datetime
    feed = importlib.import_module("feed")
    t = datetime(2026, 6, 1, 9, 15, 5)
    for sec, px in ((5, 25000.0), (20, 25010.0), (40, 24990.0), (59, 25002.0)):
        feed._note_index_bar(px, now=t.replace(second=sec))
    feed._note_index_bar(25003.0, now=t.replace(minute=16, second=1))
    bars, partial = feed.index_bars(t.date())
    assert bars[0] == (25000.0, 25010.0, 24990.0, 25002.0) and bars[1] == (25003.0,) * 4
    assert partial == {0}                                   # first tick of the day: may be incomplete -> API backfill


def test_live_spot_bars_aggregate_ticks_and_stop_at_a_gap(nep, monkeypatch):
    import time as _t
    feed = importlib.import_module("feed")
    LC = importlib.import_module("live_context").LiveContext
    d = date(2026, 6, 1)
    m1 = {j: (100.0 + j, 101.0 + j, 99.0 + j, 100.5 + j) for j in range(0, 25) if j != 13}
    monkeypatch.setattr(feed, "index_bars", lambda day=None: (m1, set()))
    ctx = LC.__new__(LC)
    ctx.today, ctx.minute = d, 24
    ctx._bars_cache, ctx._api_m1, ctx._api_day, ctx._last_backfill = {}, {}, d, _t.time()   # no API call
    b10 = ctx.spot_bars(10)
    assert b10 == [(0, 100.0, 110.0, 99.0, 109.5)]           # bar 1 (minutes 10-19) has a gap -> not returned yet
    assert [b[0] for b in ctx.spot_bars(1)] == list(range(13))
    m1[13] = (113.0, 114.0, 112.0, 113.5)                    # gap filled (e.g. by backfill) -> picked up next call
    ctx._bars_cache = {}
    assert [b[0] for b in ctx.spot_bars(10)] == [0, 1]


def test_stbt_spot_sl_catches_up_a_missed_minute(nep):
    config = importlib.import_module("config")
    S = importlib.import_module("strategies.stbt").Stbt
    d = date(2026, 6, 1)
    bars = {j: (25000.0, 25040.0, 24960.0, 25000.0) for j in range(120)}
    bars[120] = (25030.0, 25060.0, 25030.0, 25052.0)                               # UP breakout
    ctx = StbtCtx(d, [date(2026, 6, 4)], {(25250, "PE"): 230.0}, bars)
    s = S(config.STBT, lots=1, tag="T", lot_size=65)
    for m in range(0, 121):
        ctx.minute = m
        s.on_minute(ctx)
    assert len(ctx.opened) == 1
    ctx.minute = 121                                    # minute 121's candle not available yet (failed fetch)
    s.on_minute(ctx)
    assert ctx.closed == []
    ctx.bars[121] = (25000.0, 25010.0, 24900.0, 24950.0)                           # it touched the stop (24926.74)
    ctx.bars[122] = (24950.0, 24990.0, 24940.0, 24980.0)                           # the next one did not
    ctx.minute = 122
    s.on_minute(ctx)
    assert ctx.closed == [("PE", "SL_SPOT", 122)]


def test_bars_are_saved_and_reused_for_restart_and_history(nep, monkeypatch, tmp_path):
    import time as _t
    from datetime import date as _d
    config = importlib.import_module("config")
    feed = importlib.import_module("feed")
    instruments = importlib.import_module("instruments")
    LC = importlib.import_module("live_context").LiveContext
    monkeypatch.setattr(config, "BARS_DIR", tmp_path)
    d0, d1 = _d(2026, 6, 1), _d(2026, 6, 2)
    full = {j: (100.0 + j, 101.0 + j, 99.0 + j, 100.5 + j) for j in range(375)}
    monkeypatch.setattr(feed, "index_bars", lambda day=None: (full, {0}))
    c0 = LC.__new__(LC)
    c0.today, c0.minute = d0, 374
    c0._bars_cache, c0._api_m1, c0._api_day, c0._last_backfill = {}, {}, d0, _t.time()
    c0.persist_bars()
    saved = c0._load_day_bars(d0)
    assert len(saved) == 374 and 0 not in saved           # the possibly-partial first minute is not saved
    saved[0] = full[0]                                     # pretend the backfill filled it
    import json, os
    with open(os.path.join(str(tmp_path), "2026-06-01.json"), "w") as fh:
        json.dump({str(k): list(v) for k, v in saved.items()}, fh)
    calls = []
    c1 = LC.__new__(LC)
    c1.today = d1
    c1._candles = lambda *a, **k: calls.append(a) or []
    monkeypatch.setattr(instruments, "previous_trading_days", lambda d, n: [d0])
    h = c1.spot_history_bars(10, 1)
    assert calls == []                                     # no getCandleData: read from the saved file
    assert h[0] == ("2026-06-01", 0, 100.0, 110.0, 99.0, 109.5) and len(h) == 38


def test_journal_strategy_summary(nep, monkeypatch, tmp_path):
    import csv
    config = importlib.import_module("config")
    monkeypatch.setattr(config, "JOURNAL_FILE", tmp_path / "journal.csv")
    journal = importlib.import_module("state.journal")
    with open(config.JOURNAL_FILE, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=journal.FIELDS)
        w.writeheader()
        for s, dt, n in (("st10", "2026-11-27 10:00:00", -1200.0), ("stbt", "2026-11-27 09:35:00", 3400.0),
                         ("btst9", "2026-11-26 09:20:00", -300.0)):
            w.writerow(dict(strategy=s, exit_dt=dt, gross_rs=n + 50, charges_rs=50, net_rs=n))
    day = journal.summary("2026-11-27", "2026-11-27")
    assert set(day) == {"st10", "stbt"} and day["stbt"]["net"] == 3400.0 and day["st10"]["wins"] == 0
    assert journal.summary()["btst9"]["trades"] == 1
    assert "TOTAL" in journal.format_summary(day, "x")
