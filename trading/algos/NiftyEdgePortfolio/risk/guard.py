"""
Portfolio risk controls.

  can_enter()      halt flag, daily-loss block, market window, margin check
  after_minute()   daily loss limit (BLOCK_NEW or FLATTEN), portfolio drawdown stop (flatten + HALT)
  intrabar()       disaster stops on live LTP + detection of filled resting broker SLs
  reconcile()      manual-trade guard: broker NIFTY option positions vs what this algo holds
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import config
import feed
import instruments
from broker import orders
from state import journal, store

_alerted = set()
_mismatch_since: dict[str, float] = {}


def _alert_once(key, msg):
    if key not in _alerted:
        _alerted.add(key)
        print(msg)


def open_mtm(ctx) -> float:
    tot = 0.0
    for leg in store.legs(ctx.st):
        ltp = config.ltp.get(leg.token)
        if ltp is not None and leg.entry_price is not None:
            tot += leg.pnl_points(ltp) * leg.qty
    return tot


def can_enter(ctx, strategy: str, lots: int) -> bool:
    if os.path.exists(config.HALT_FILE):
        _alert_once("halt", f"[RISK] HALTED ({config.HALT_FILE} exists) - no new entries. Delete the file to resume.")
        return False
    if ctx.block_new:
        return False
    now = datetime.now()
    if not ((now.hour, now.minute) >= config.MARKET_OPEN and (now.hour, now.minute) <= (15, 29) and not (now.hour == 15 and now.minute == 29 and now.second > 30)):
        print(f"[RISK] {strategy}: outside market hours - entry skipped")
        return False
    if config.MARGIN_CHECK:
        need = lots * config.STRATEGIES[strategy]["margin_per_lot"] * config.MARGIN_BUFFER
        avail = orders.available_margin()
        if avail is not None and avail < need:
            print(f"[RISK] {strategy}: margin {avail:,.0f} < needed {need:,.0f} - entry skipped")
            return False
    return True


def flatten_all(ctx, reason):
    print(f"[EMERGENCY] {reason}: squaring off ALL legs")
    for leg in store.legs(ctx.st):
        ctx.close_leg(leg, f"EMERGENCY_{reason}")


def after_minute(ctx):
    day = journal.realised_today(ctx.today) + open_mtm(ctx)
    ctx.day_pnl = day
    if day <= -abs(config.DAILY_LOSS_LIMIT):
        if config.DAILY_LOSS_ACTION == "FLATTEN":
            _alert_once(f"dl-{ctx.today}", f"[RISK] day P&L {day:,.0f} <= -{config.DAILY_LOSS_LIMIT:,} -> FLATTEN")
            flatten_all(ctx, "DAILY_LOSS")
        else:
            _alert_once(f"dl-{ctx.today}", f"[RISK] day P&L {day:,.0f} <= -{config.DAILY_LOSS_LIMIT:,} -> no new entries today (open legs keep their rules)")
        ctx.block_new = True
    eq = ctx.st["equity"]
    dd = eq["cum"] + open_mtm(ctx) - eq["peak"]
    if dd <= -abs(config.PORTFOLIO_DD_STOP):
        _alert_once("pdd", f"[RISK] portfolio drawdown {dd:,.0f} <= -{config.PORTFOLIO_DD_STOP:,} -> FLATTEN + HALT")
        flatten_all(ctx, "PORTFOLIO_DD")
        os.makedirs(config.RUNTIME_DIR, exist_ok=True)
        with open(config.HALT_FILE, "w") as fh:
            fh.write(f"halted {datetime.now():%Y-%m-%d %H:%M:%S} drawdown {dd:,.0f}\n")
        ctx.block_new = True


_last_sl_poll = 0.0


def intrabar(ctx):
    """Every ~2 s: disaster stop on live LTP; every ~10 s: has a resting broker SL filled?"""
    global _last_sl_poll
    poll_sl = time.monotonic() - _last_sl_poll >= 10
    if poll_sl:
        _last_sl_poll = time.monotonic()
    for leg in store.legs(ctx.st):
        if leg.id in {d["id"] for d in ctx.st["deferred_exits"]}:
            continue
        if poll_sl and leg.broker_sl_oid and not config.DRY_RUN:
            status, avg = orders.sl_status(leg.broker_sl_oid)
            if status == "complete":
                ctx.record_broker_sl_fill(leg, avg)
                continue
        ltp = config.ltp.get(leg.token)
        if leg.disaster_sl and ltp is not None and ltp >= leg.disaster_sl:
            print(f"[RISK] {leg.strategy} {leg.symbol} LTP {ltp} >= disaster stop {leg.disaster_sl}")
            ctx.close_leg(leg, "DISASTER_SL")


def reconcile(ctx):
    """Manual-trade guard. Any NIFTY option quantity at the broker that this algo
    did not open is a manual (or foreign) trade."""
    mg = config.MANUAL_GUARD
    if not mg.get("enabled"):
        return
    if config.DRY_RUN:
        positions = orders.refresh_positions() if config.objconn else []
    else:
        positions = orders.refresh_positions(force=True)
    expected = {}
    for leg in store.legs(ctx.st):
        expected[leg.token] = expected.get(leg.token, 0) + (-leg.qty if leg.side == "SELL" else leg.qty)
    actual = {}
    for p in positions or []:
        tok = str(p.get("symboltoken"))
        if instruments.by_token(tok) is None:
            continue                       # not a NIFTY option -> not ours to judge
        try:
            actual[tok] = actual.get(tok, 0) + int(float(p.get("netqty") or 0))
        except Exception:
            pass
    now = time.monotonic()
    for tok in set(expected) | set(actual):
        diff = actual.get(tok, 0) - (0 if config.DRY_RUN else expected.get(tok, 0))
        if diff == 0 or orders.has_pending(tok):
            _mismatch_since.pop(tok, None)
            continue
        first = _mismatch_since.setdefault(tok, now)
        if now - first < mg.get("grace_seconds", 120):
            continue
        info = instruments.by_token(tok)
        sym = info[3] if info else tok
        print(f"[GUARD] MANUAL/UNKNOWN position {sym}: broker {actual.get(tok, 0)} vs algo {expected.get(tok, 0)} (diff {diff})")
        if mg.get("auto_exit") and not config.DRY_RUN:
            side = "BUY" if diff < 0 else "SELL"
            q, avg = orders.execute(sym, tok, abs(diff), side, "NEP_GUARD")
            print(f"[GUARD] auto-exit {side} {sym} qty={q} @ {avg}")
        _mismatch_since[tok] = now + 1e9   # alert once per mismatch episode
