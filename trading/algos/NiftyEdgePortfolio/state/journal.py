"""Closed-trade journal (CSV) with charges, and equity / drawdown tracking."""
from __future__ import annotations

import csv
import os
from datetime import date

import config
from context import Leg, charges

FIELDS = ["strategy", "tag", "entry_dt", "exit_dt", "expiry", "strike", "opt", "qty", "entry", "exit", "reason",
          "gross_rs", "charges_rs", "net_rs", "mode"]


def record(st: dict, leg: Leg) -> float:
    """Append the closed leg to journal.csv, update equity. Returns net Rs."""
    gross = leg.pnl_points(leg.exit_price) * leg.qty if leg.exit_price is not None else 0.0
    d_in = date.fromisoformat(leg.entry_dt[:10])
    d_out = date.fromisoformat(leg.exit_dt[:10])
    entry_side = "SELL" if leg.side == "SELL" else "BUY"
    exit_side = "BUY" if leg.side == "SELL" else "SELL"
    ch = charges(leg.entry_price, leg.qty, entry_side, d_in) + (charges(leg.exit_price, leg.qty, exit_side, d_out) if leg.exit_price else 0)
    net = gross - ch
    os.makedirs(config.RUNTIME_DIR, exist_ok=True)
    new = not os.path.exists(config.JOURNAL_FILE)
    with open(config.JOURNAL_FILE, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(dict(strategy=leg.strategy, tag=leg.tag, entry_dt=leg.entry_dt, exit_dt=leg.exit_dt, expiry=leg.expiry,
                        strike=leg.strike, opt=leg.opt, qty=leg.qty, entry=leg.entry_price, exit=leg.exit_price,
                        reason=leg.exit_reason, gross_rs=round(gross, 2), charges_rs=round(ch, 2), net_rs=round(net, 2),
                        mode="PAPER" if config.DRY_RUN else "LIVE"))
    eq = st["equity"]
    eq["cum"] = round(eq["cum"] + net, 2)
    eq["peak"] = max(eq["peak"], eq["cum"])
    return net


def realised_today(today: date) -> float:
    if not os.path.exists(config.JOURNAL_FILE):
        return 0.0
    tot = 0.0
    with open(config.JOURNAL_FILE) as fh:
        for r in csv.DictReader(fh):
            if r["exit_dt"][:10] == today.isoformat():
                tot += float(r["net_rs"] or 0)
    return tot


def summary(frm: str | None = None, to: str | None = None) -> dict:
    """Strategy-wise realised P&L from journal.csv for exit dates frm..to (YYYY-MM-DD, inclusive; None = open-ended)."""
    out = {}
    if not os.path.exists(config.JOURNAL_FILE):
        return out
    with open(config.JOURNAL_FILE) as fh:
        for r in csv.DictReader(fh):
            d = r["exit_dt"][:10]
            if (frm and d < frm) or (to and d > to):
                continue
            s = out.setdefault(r["strategy"], {"trades": 0, "wins": 0, "gross": 0.0, "charges": 0.0, "net": 0.0})
            net = float(r["net_rs"] or 0)
            s["trades"] += 1
            s["wins"] += net > 0
            s["gross"] += float(r["gross_rs"] or 0)
            s["charges"] += float(r["charges_rs"] or 0)
            s["net"] += net
    return out


def format_summary(summ: dict, title: str) -> str:
    lines = [title, f"  {'strategy':<12}{'trades':>7}{'win%':>7}{'gross':>12}{'charges':>10}{'net':>12}"]
    tot = {"trades": 0, "wins": 0, "gross": 0.0, "charges": 0.0, "net": 0.0}
    for name in sorted(summ):
        s = summ[name]
        for k in tot:
            tot[k] += s[k]
        lines.append(f"  {name:<12}{s['trades']:>7}{(100 * s['wins'] / s['trades'] if s['trades'] else 0):>6.0f}%"
                     f"{s['gross']:>12,.0f}{s['charges']:>10,.0f}{s['net']:>12,.0f}")
    lines.append(f"  {'TOTAL':<12}{tot['trades']:>7}{(100 * tot['wins'] / tot['trades'] if tot['trades'] else 0):>6.0f}%"
                 f"{tot['gross']:>12,.0f}{tot['charges']:>10,.0f}{tot['net']:>12,.0f}")
    return "\n".join(lines)
