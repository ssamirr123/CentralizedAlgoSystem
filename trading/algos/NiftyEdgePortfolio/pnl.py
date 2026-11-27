#!/usr/bin/env python3
"""Strategy-wise P&L for NiftyEdgePortfolio (reads runtime/journal.csv and runtime/state.json; no broker login).

  python pnl.py                 today
  python pnl.py --day 2026-11-27
  python pnl.py --week          last 7 days
  python pnl.py --month         this month
  python pnl.py --all           since the start
  python pnl.py --from 2026-12-01 --to 2026-12-31
Realised P&L = closed legs after charges. Open legs are listed separately (their MTM is in the live log / control centre)."""
import argparse
import json
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
from state import journal  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--week", action="store_true")
    ap.add_argument("--month", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    a = ap.parse_args()
    t = date.today()
    if a.all:
        frm, to, title = None, None, "all time"
    elif a.frm or a.to:
        frm, to, title = a.frm, a.to, f"{a.frm or 'start'} -> {a.to or 'today'}"
    elif a.week:
        frm, to, title = (t - timedelta(days=6)).isoformat(), t.isoformat(), "last 7 days"
    elif a.month:
        frm, to, title = t.replace(day=1).isoformat(), t.isoformat(), t.strftime("%B %Y")
    else:
        d = a.day or t.isoformat()
        frm, to, title = d, d, d
    print(journal.format_summary(journal.summary(frm, to), f"Realised P&L by strategy ({title})"))
    try:
        with open(config.STATE_FILE) as fh:
            st = json.load(fh)
        legs = [l for l in st.get("legs", []) if l.get("status") == "OPEN"]
        print(f"\nOpen legs ({len(legs)}):")
        for l in legs:
            print(f"  {l['strategy']:<12}{l.get('symbol', ''):<26} qty {l['qty']:>4}  sold @ {l['entry_price']}  since {l['entry_dt']}")
        eq = st.get("equity", {})
        if eq:
            print(f"\nCumulative realised (all strategies): {eq.get('cum', 0):,.0f}   peak: {eq.get('peak', 0):,.0f}")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
