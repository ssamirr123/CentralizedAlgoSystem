import type { MarketIndexQuote } from "@/api/types";

const LABEL: Record<string, string> = {
  NIFTY: "NIFTY 50",
  BANKNIFTY: "BANK NIFTY",
  INDIA_VIX: "INDIA VIX",
  SENSEX: "SENSEX",
};

function fmt(n: number | null): string {
  return n == null ? "—" : n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export function IndexCard({ q }: { q: MarketIndexQuote }) {
  const up = (q.change ?? 0) >= 0;
  const sign = q.change == null ? "zero" : up ? "pos" : "neg";
  return (
    <div className="card index-card">
      <div className="index-card-head">
        <span className="index-card-name">{LABEL[q.symbol] ?? q.symbol}</span>
        <span className={`badge ${q.status === "live" ? "running" : q.status === "stale" ? "stale" : "unknown"}`}>
          <span className="dot" />
          {q.status.toUpperCase()}
        </span>
      </div>
      <div className={`index-card-ltp ${sign}`}>{fmt(q.ltp)}</div>
      <div className={`index-card-chg ${sign}`}>
        {q.change == null ? "—" : `${up ? "+" : ""}${fmt(q.change)} (${q.change_percent?.toFixed(2)}%)`}
      </div>
      <div className="index-card-ohlc">
        <span>O {fmt(q.open)}</span>
        <span>H {fmt(q.high)}</span>
        <span>L {fmt(q.low)}</span>
        <span>PC {fmt(q.prev_close)}</span>
      </div>
    </div>
  );
}
