import { useMemo } from "react";
import type { StraddleSessionOI } from "@/api/types";
import { formatTimeIST, parseApiTimestampMs } from "../format";
import { ChartCanvas, type ChartSeries } from "./ChartCanvas";
import type { SharedChartState } from "./DailyStraddleChart";

function pcrSentiment(pcr: number | null): { label: string; badge: string; tone: "pos" | "neg" | "flat" } {
  if (pcr == null) return { label: "—", badge: "No data", tone: "flat" };
  if (pcr < 0.7) return { label: "call-heavy", badge: "Bearish OI", tone: "neg" };
  if (pcr > 1.3) return { label: "put-heavy", badge: "Bullish OI", tone: "pos" };
  return { label: "balanced", badge: "Neutral OI", tone: "flat" };
}

const LAKH = 100000;

export function OIBuildupChart({ oi, shared }: { oi: StraddleSessionOI; shared: SharedChartState }) {
  const points = useMemo(
    () => oi.points.map((p) => ({ ...p, t: parseApiTimestampMs(p.timestamp) })),
    [oi.points],
  );

  const fullDomain: [number, number] = points.length
    ? [points[0].t, points[points.length - 1].t]
    : [Date.now() - 1, Date.now()];

  const callTotal: ChartSeries = {
    id: "call-total", axis: "left", color: "#f87171",
    data: points.map((p) => ({ t: p.t, v: p.call_oi_total / LAKH })),
  };
  const putTotal: ChartSeries = {
    id: "put-total", axis: "left", color: "#4ade80",
    data: points.map((p) => ({ t: p.t, v: p.put_oi_total / LAKH })),
  };
  const callIntra: ChartSeries = {
    id: "call-intra", axis: "left", color: "#f87171", dash: [2, 2], width: 1,
    data: points.map((p) => ({ t: p.t, v: p.call_oi_change / LAKH })),
  };
  const putIntra: ChartSeries = {
    id: "put-intra", axis: "left", color: "#4ade80", dash: [2, 2], width: 1,
    data: points.map((p) => ({ t: p.t, v: p.put_oi_change / LAKH })),
  };

  const latest = points.length ? points[points.length - 1] : null;
  const hover = nearest(points, shared.hoverT);
  const row = hover ?? latest;
  const sentiment = pcrSentiment(latest?.pcr ?? null);

  return (
    <div className="card">
      <div className="toolbar">
        <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
          Intraday OI build-up · {oi.trading_date} (lakh)
        </div>
        <span
          className="conn"
          style={{ marginLeft: "auto", color: sentiment.tone === "pos" ? "var(--pos)" : sentiment.tone === "neg" ? "var(--neg)" : undefined }}
        >
          {sentiment.tone === "pos" ? "▲" : sentiment.tone === "neg" ? "▼" : "•"} {sentiment.badge} · PCR {latest?.pcr?.toFixed(2) ?? "—"}
        </span>
      </div>
      {row && (
        <div style={{ padding: "0 4px 4px", fontSize: 12, color: "var(--text-dim)" }}>
          {formatTimeIST(row.t)}
          {" "}Put {(row.put_oi_total / LAKH).toFixed(1)}L Call {(row.call_oi_total / LAKH).toFixed(1)}L
          {" · intra P "}{(row.put_oi_change / LAKH >= 0 ? "+" : "")}{(row.put_oi_change / LAKH).toFixed(1)}
          {" · C "}{(row.call_oi_change / LAKH >= 0 ? "+" : "")}{(row.call_oi_change / LAKH).toFixed(1)}
        </div>
      )}
      {points.length === 0 ? (
        <div className="state">No OI snapshots yet for this session.</div>
      ) : (
        <ChartCanvas
          series={[callTotal, putTotal, callIntra, putIntra]}
          xDomain={shared.xDomain}
          onXDomainChange={shared.onXDomainChange}
          fullDomain={fullDomain}
          hoverT={shared.hoverT}
          onHoverChange={shared.onHoverChange}
          leftAxisFormat={(v) => `${v.toFixed(0)}L`}
          rightAxisFormat={() => ""}
        />
      )}
    </div>
  );
}

function nearest<T extends { t: number }>(points: T[], t: number | null): T | null {
  if (t == null || points.length === 0) return null;
  let best = points[0];
  let bestDiff = Math.abs(best.t - t);
  for (const p of points) {
    const diff = Math.abs(p.t - t);
    if (diff < bestDiff) {
      best = p;
      bestDiff = diff;
    }
  }
  return best;
}
