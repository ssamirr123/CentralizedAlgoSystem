import { useMemo, useState } from "react";
import type { StraddleSessionChart } from "@/api/types";
import { formatTimeIST, istWallClockToEpoch } from "../format";
import { buildStraddleSeries } from "../straddleSeries";
import { ChartCanvas, type ChartSeries, type DrawMode } from "./ChartCanvas";
import { ema, vwap } from "./indicators";

const LOCK_HOUR = 9;
const LOCK_MIN = 16;

export interface SharedChartState {
  xDomain: [number, number];
  onXDomainChange: (d: [number, number]) => void;
  hoverT: number | null;
  onHoverChange: (t: number | null) => void;
}

export function DailyStraddleChart({ chart, shared }: { chart: StraddleSessionChart; shared: SharedChartState }) {
  const points = useMemo(
    () => buildStraddleSeries(chart.spot, chart.atm_ce, chart.atm_pe),
    [chart],
  );

  const [showSpot, setShowSpot] = useState(true);
  const [showEma9, setShowEma9] = useState(false);
  const [showEma21, setShowEma21] = useState(false);
  const [showVwap, setShowVwap] = useState(false);
  const [drawMode, setDrawMode] = useState<DrawMode>("none");
  const [hLines, setHLines] = useState<number[]>([]);
  const [vLines, setVLines] = useState<number[]>([]);

  const fullDomain: [number, number] = points.length
    ? [points[0].t, points[points.length - 1].t]
    : [Date.now() - 1, Date.now()];

  const straddleLine = points.map((p) => ({ t: p.t, v: p.v }));
  const spotLine = points.map((p) => ({ t: p.t, v: p.spot }));

  const series: ChartSeries[] = [
    { id: "straddle", data: straddleLine, color: "#2dd4bf", axis: "left", width: 1.75 },
  ];
  if (showSpot) series.push({ id: "spot", data: spotLine, color: "#cbd5e1", axis: "right", width: 1.25 });
  if (showEma9) series.push({ id: "ema9", data: ema(straddleLine, 9), color: "#f59e0b", axis: "left", dash: [5, 3] });
  if (showEma21) series.push({ id: "ema21", data: ema(straddleLine, 21), color: "#a78bfa", axis: "left", dash: [5, 3] });
  if (showVwap) {
    series.push({
      id: "vwap",
      data: vwap(points.map((p) => ({ t: p.t, v: p.v, volume: p.volume }))),
      color: "#38bdf8",
      axis: "left",
      dash: [2, 2],
    });
  }

  const lockTimeMs = istWallClockToEpoch(chart.trading_date, LOCK_HOUR, LOCK_MIN);

  const hover = points.length ? nearest(points, shared.hoverT) : null;

  return (
    <div className="card">
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <label className="chk"><input type="checkbox" checked={showSpot} onChange={(e) => setShowSpot(e.target.checked)} /> Spot</label>
        <label className="chk"><input type="checkbox" checked={showEma9} onChange={(e) => setShowEma9(e.target.checked)} /> EMA 9</label>
        <label className="chk"><input type="checkbox" checked={showEma21} onChange={(e) => setShowEma21(e.target.checked)} /> EMA 21</label>
        <label className="chk"><input type="checkbox" checked={showVwap} onChange={(e) => setShowVwap(e.target.checked)} /> VWAP</label>
        <span style={{ width: 1, height: 18, background: "var(--border)" }} />
        <button className={`sm ${drawMode === "hline" ? "active" : ""}`} onClick={() => setDrawMode(drawMode === "hline" ? "none" : "hline")}>H-line</button>
        <button className={`sm ${drawMode === "vline" ? "active" : ""}`} onClick={() => setDrawMode(drawMode === "vline" ? "none" : "vline")}>V-line</button>
        <button className="sm" onClick={() => { setHLines([]); setVLines([]); }}>Clear</button>
        <button className="sm" onClick={() => shared.onXDomainChange(fullDomain)}>Fit</button>
        {hover && (
          <span className="conn" style={{ marginLeft: "auto" }}>
            {formatTimeIST(hover.t)}
            {" · "}straddle ₹{hover.v.toFixed(1)} · spot {hover.spot.toFixed(2)} · C {hover.ce.toFixed(1)} · P {hover.pe.toFixed(1)}
          </span>
        )}
      </div>
      <div style={{ padding: "4px 4px 0", fontSize: 12, color: "var(--text-dim)" }}>
        Daily ATM straddle (₹) · spot overlay {chart.atm_strike != null ? `· ATM ${chart.atm_strike}` : ""}
      </div>
      {points.length === 0 ? (
        <div className="state">No candles yet for this session's locked ATM contracts.</div>
      ) : (
        <ChartCanvas
          series={series}
          xDomain={shared.xDomain}
          onXDomainChange={shared.onXDomainChange}
          fullDomain={fullDomain}
          hoverT={shared.hoverT}
          onHoverChange={shared.onHoverChange}
          vRefLines={[{ t: lockTimeMs, label: "09:16", color: "#94a3b8" }]}
          hLines={hLines}
          vLines={vLines}
          drawMode={drawMode}
          onAddHLine={(v) => setHLines((prev) => [...prev, v])}
          onAddVLine={(t) => setVLines((prev) => [...prev, t])}
          leftAxisFormat={(v) => `₹${v.toFixed(0)}`}
          rightAxisFormat={(v) => v.toFixed(0)}
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
