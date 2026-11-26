"""
Compare replay trades (the LIVE strategy code) with the research backtest trades.

  python -m replay.compare_backtest --replay replay/replay_trades.csv \
      --research "/Users/samirkuila/Trading Strategy/Obsidian/NIFTY/NIFTY_DTE_EDGE_RESEARCH/results"

A trade matches when entry date, side (CE/PE), strike, entry time, exit date and
exit time are identical and entry/exit prices agree to 0.05.
"""
from __future__ import annotations

import argparse
import os

import pandas as pd

REASON = {  # replay reason -> research reason
    "TIME_1515": "TIME", "SL": "SL", "TIME_EXPIRY_1515": "TIME_EXPIRY_1515", "TIME_0920": "TIME_0920",
    "FLIP": "FLIP", "ROLL": "ROLL",
}


def research_trades(res: str) -> dict[str, pd.DataFrame]:
    out = {}
    p10 = pd.read_csv(os.path.join(res, "P10_strangle60", "legs.csv.gz"))
    p10 = p10[p10.exit_cfg == 1515]
    out["strangle60"] = pd.DataFrame(dict(entry_date=p10.date, leg=p10.leg, strike=p10.strike, entry_time=p10.entry_time, exit_date=p10.date,
                                          exit_time=p10.exit_time, entry=p10.entry, exit=p10.exit, why=p10.why))
    p14 = pd.read_csv(os.path.join(res, "P14_positional", "trades.csv.gz"))
    for strat, name in (("OTM4_1day", "otm4_1day"), ("BTST9", "btst9")):
        x = p14[p14.strat == strat]
        out[name] = pd.DataFrame(dict(entry_date=x.date, leg=x.leg, strike=x.strike, entry_time=x.entry_time, exit_date=x.exit_date,
                                      exit_time=x.exit_time, entry=x.entry, exit=x.exit, why=x.why))
    p12 = pd.read_csv(os.path.join(res, "P12_investoruday", "trades.csv.gz"))
    x = p12[p12.strat == "ST10"]          # identical to P13 cfg H0, but with entry/exit times
    out["st10"] = pd.DataFrame(dict(entry_date=x.date, leg=x.leg, strike=x.strike, entry_time=x.entry_time, exit_date=x.exit_date,
                                    exit_time=x.exit_time, entry=x.entry, exit=x.exit, why=x.why))
    return out


def compare(rep: pd.DataFrame, bt: pd.DataFrame, start: str, end: str):
    key = ["entry_date", "leg", "strike", "entry_time"]
    r = rep.copy()
    r = r[(r.entry_date >= start) & (r.entry_date <= end)]
    b = bt[(bt.entry_date >= start) & (bt.entry_date <= end)].copy()
    if len(r):   # ignore research trades before the replay's first entry (indicator warm-up / flat start)
        b = b[b.entry_date + " " + b.entry_time >= r.entry_date.min() + " " + r[r.entry_date == r.entry_date.min()].entry_time.min()]
    for df in (r, b):
        df["strike"] = df.strike.astype(int)
    j = r.merge(b, on=key, how="outer", suffixes=("_live", "_bt"), indicator=True)
    both = j[j._merge == "both"].copy()
    both["ok"] = ((both.exit_date_live == both.exit_date_bt) & (both.exit_time_live == both.exit_time_bt)
                  & ((both.entry_live - both.entry_bt).abs() <= 0.05) & ((both.exit_live - both.exit_bt).abs() <= 0.05))
    return dict(replay=len(r), backtest=len(b), matched=int(both.ok.sum()), mismatched=both[~both.ok],
                only_live=j[j._merge == "left_only"], only_bt=j[j._merge == "right_only"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", required=True)
    ap.add_argument("--research", required=True)
    ap.add_argument("--from", dest="start", default="2021-01-01")
    ap.add_argument("--to", dest="end", default="2099-12-31")
    a = ap.parse_args()
    rep = pd.read_csv(a.replay)
    bt = research_trades(a.research)
    pd.set_option("display.width", 250)
    for name, g in rep.groupby("strategy"):
        g = g.rename(columns={})
        c = compare(g, bt[name], a.start, a.end)
        pct = 100 * c["matched"] / max(1, max(c["replay"], c["backtest"]))
        print(f"\n=== {name}: replay {c['replay']} trades | backtest {c['backtest']} | exact matches {c['matched']} ({pct:.1f}%)")
        for lab in ("mismatched", "only_live", "only_bt"):
            df = c[lab]
            if len(df):
                print(f"--- {lab}: {len(df)}")
                print(df.head(8).to_string(index=False))


if __name__ == "__main__":
    main()
