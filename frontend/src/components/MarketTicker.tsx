import { Link } from "react-router-dom";
import { useMarketIndices } from "@/api/hooks";
import type { MarketIndexQuote } from "@/api/types";

const LABEL: Record<string, string> = {
  NIFTY: "NIFTY",
  BANKNIFTY: "BANKNIFTY",
  INDIA_VIX: "VIX",
  SENSEX: "SENSEX",
};
const ORDER = ["NIFTY", "BANKNIFTY", "INDIA_VIX", "SENSEX"];

function fmt(n: number | null): string {
  return n == null ? "—" : n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function Chip({ q }: { q: MarketIndexQuote }) {
  const up = (q.change ?? 0) >= 0;
  const cls = q.change == null ? "zero" : up ? "pos" : "neg";
  const stale = q.status === "stale" || q.status === "no_data";
  return (
    <span className="mkt-chip" title={`${q.symbol} · ${q.status}`} style={{ opacity: stale ? 0.55 : 1 }}>
      <span className="mkt-chip-label">{LABEL[q.symbol] ?? q.symbol}</span>
      <span className="mkt-chip-ltp">{fmt(q.ltp)}</span>
      <span className={`mkt-chip-chg ${cls}`}>
        {q.change == null ? "" : `${up ? "▲" : "▼"} ${q.change_percent?.toFixed(2) ?? "—"}%`}
      </span>
    </span>
  );
}

/** Compact always-visible index strip for the top bar. */
export function MarketTicker() {
  const { data } = useMarketIndices();
  if (!data || data.length === 0) return null;
  const byId = Object.fromEntries(data.map((q) => [q.symbol, q]));
  return (
    <Link to="/market" className="mkt-ticker" title="Open the Market screen">
      {ORDER.filter((s) => byId[s]).map((s) => (
        <Chip key={s} q={byId[s]} />
      ))}
    </Link>
  );
}
