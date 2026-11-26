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
