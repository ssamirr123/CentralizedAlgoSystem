import { useMemo, useState } from "react";
import { useMarketCandles } from "@/api/hooks";

const SYMBOLS = ["NIFTY", "BANKNIFTY", "INDIA_VIX", "SENSEX"];

/** Lightweight inline-SVG close-price line for one index's 1-minute candles.
 *  No charting library — keeps the bundle small. */
export function MarketChart() {
  const [symbol, setSymbol] = useState("NIFTY");
  const candles = useMarketCandles(symbol);

  const path = useMemo(() => {
    const data = candles.data ?? [];
    if (data.length < 2) return null;
    const closes = data.map((c) => c.close);
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const span = max - min || 1;
    const w = 640;
    const h = 160;
    const step = w / (closes.length - 1);
    const pts = closes.map((c, i) => `${(i * step).toFixed(1)},${(h - ((c - min) / span) * h).toFixed(1)}`);
    return { d: `M ${pts.join(" L ")}`, min, max, first: closes[0], last: closes[closes.length - 1], w, h };
  }, [candles.data]);

  return (
    <div className="card">
      <div className="toolbar">
        <div className="field">
          <label>Index</label>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {SYMBOLS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <button className="sm" onClick={() => candles.refetch()}>
          Refresh
        </button>
        {path && (
          <span className="conn" style={{ marginLeft: "auto" }}>
            {path.last >= path.first ? "▲" : "▼"} {path.first.toFixed(2)} → {path.last.toFixed(2)}
          </span>
        )}
      </div>
      {path ? (
        <svg viewBox={`0 0 ${path.w} ${path.h}`} width="100%" height={180} preserveAspectRatio="none">
          <path
            d={path.d}
            fill="none"
            stroke={path.last >= path.first ? "var(--pos)" : "var(--neg)"}
            strokeWidth={1.5}
          />
        </svg>
      ) : (
        <div className="state">
          {candles.isLoading ? "Loading…" : "No 1-minute candles yet (populated during market hours)."}
        </div>
      )}
    </div>
  );
}
