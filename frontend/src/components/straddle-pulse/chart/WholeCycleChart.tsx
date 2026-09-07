import { useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import * as api from "@/api/endpoints";
import type { StraddleSession } from "@/api/types";
import { formatDateLabel } from "../format";
import { buildStraddleSeries } from "../straddleSeries";
import { ChartCanvas, type ChartSeries } from "./ChartCanvas";

/** Concatenates each trading day's own locked-ATM straddle+spot series
 * (never a single blended weekly ATM -- each segment comes from that
 * day's own session/chart data) with a vertical separator + day label at
 * every session boundary, per spec section 6/23. */
export function WholeCycleChart({ sessions }: { sessions: StraddleSession[] }) {
  const charts = useQueries({
    queries: sessions.map((s) => ({
      queryKey: ["straddle-session-chart", s.id],
      queryFn: () => api.getSessionChart(s.id),
    })),
  });

  const [xDomain, setXDomain] = useState<[number, number] | null>(null);
  const [hoverT, setHoverT] = useState<number | null>(null);

  const { straddleLine, spotLine, dayMarkers, fullDomain } = useMemo(() => {
    const straddle: { t: number; v: number }[] = [];
    const spot: { t: number; v: number }[] = [];
    const markers: { t: number; label: string }[] = [];
    for (let i = 0; i < sessions.length; i++) {
      const data = charts[i]?.data;
      if (!data) continue;
      const points = buildStraddleSeries(data.spot, data.atm_ce, data.atm_pe);
      if (points.length === 0) continue;
      markers.push({ t: points[0].t, label: formatDateLabel(sessions[i].trading_date, { withMonth: false }) });
      for (const p of points) {
        straddle.push({ t: p.t, v: p.v });
        spot.push({ t: p.t, v: p.spot });
      }
    }
    const domain: [number, number] = straddle.length
      ? [straddle[0].t, straddle[straddle.length - 1].t]
      : [Date.now() - 1, Date.now()];
    return { straddleLine: straddle, spotLine: spot, dayMarkers: markers, fullDomain: domain };
  }, [sessions, charts.map((c) => c.data).join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  const view = xDomain ?? fullDomain;
  const loading = charts.some((c) => c.isLoading);

  const series: ChartSeries[] = [
    { id: "straddle", data: straddleLine, color: "#2dd4bf", axis: "left", width: 1.5 },
    { id: "spot", data: spotLine, color: "#cbd5e1", axis: "right", width: 1 },
  ];

  return (
    <div className="card">
      <div className="toolbar">
        <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
          Whole cycle · ATM straddle (₹) · spot overlay -- dashed lines mark each day's own locked ATM, never one blended weekly ATM
        </div>
        <button className="sm" style={{ marginLeft: "auto" }} onClick={() => setXDomain(fullDomain)}>
          Fit
        </button>
      </div>
      {straddleLine.length === 0 ? (
        <div className="state">{loading ? "Loading…" : "No trading sessions with chart data yet."}</div>
      ) : (
        <ChartCanvas
          series={series}
          xDomain={view}
          onXDomainChange={setXDomain}
          fullDomain={fullDomain}
          hoverT={hoverT}
          onHoverChange={setHoverT}
          vRefLines={dayMarkers.map((m) => ({ t: m.t, label: m.label, color: "#64748b" }))}
          leftAxisFormat={(v) => `₹${v.toFixed(0)}`}
          rightAxisFormat={(v) => v.toFixed(0)}
        />
      )}
    </div>
  );
}
