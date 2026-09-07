import { useCallback, useEffect, useRef } from "react";
import { formatTimeIST } from "../format";

export interface ChartPoint {
  t: number; // ms epoch
  v: number;
}

export interface ChartSeries {
  id: string;
  data: ChartPoint[];
  color: string;
  axis: "left" | "right";
  dash?: number[];
  width?: number;
}

export type DrawMode = "none" | "hline" | "vline";

export interface ChartCanvasProps {
  height?: number;
  series: ChartSeries[];
  /** [minT, maxT] currently visible. */
  xDomain: [number, number];
  onXDomainChange: (d: [number, number]) => void;
  /** Full data domain -- "Fit" resets xDomain to this. */
  fullDomain: [number, number];
  hoverT: number | null;
  onHoverChange: (t: number | null) => void;
  vRefLines?: { t: number; label?: string; color?: string }[];
  hLines?: number[];
  vLines?: number[];
  drawMode?: DrawMode;
  onAddHLine?: (v: number) => void;
  onAddVLine?: (t: number) => void;
  leftAxisFormat?: (v: number) => string;
  rightAxisFormat?: (v: number) => string;
  leftDomain?: [number, number];
  rightDomain?: [number, number];
}

const PAD = { left: 56, right: 56, top: 10, bottom: 22 };
// Wider than any real gap between consecutive 1-minute candles within a
// trading session; narrower than the overnight/weekend gap between two
// trading days -- the threshold that tells a real data gap apart from a
// day boundary in a multi-day (Whole Cycle) series.
const GAP_BREAK_MS = 5 * 60 * 1000;

function niceDomain(values: number[]): [number, number] {
  if (values.length === 0) return [0, 1];
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (lo === hi) {
    lo -= 1;
    hi += 1;
  }
  const pad = (hi - lo) * 0.08;
  return [lo - pad, hi + pad];
}

function fmtTime(t: number): string {
  return formatTimeIST(t);
}

/** Dependency-free canvas line chart: pan/zoom, synced crosshair (via
 * controlled hoverT/xDomain props), and H-line/V-line drawing. No
 * charting library, matching this project's existing convention. */
export function ChartCanvas(props: ChartCanvasProps) {
  const {
    height = 220, series, xDomain, onXDomainChange, fullDomain, hoverT, onHoverChange,
    vRefLines = [], hLines = [], vLines = [], drawMode = "none", onAddHLine, onAddVLine,
    leftAxisFormat = (v) => v.toFixed(2), rightAxisFormat = (v) => v.toFixed(2),
    leftDomain: leftDomainProp, rightDomain: rightDomainProp,
  } = props;

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ startX: number; startDomain: [number, number] } | null>(null);

  const leftVals = series.filter((s) => s.axis === "left").flatMap((s) => s.data.map((p) => p.v));
  const rightVals = series.filter((s) => s.axis === "right").flatMap((s) => s.data.map((p) => p.v));
  const leftDomain = leftDomainProp ?? niceDomain(leftVals);
  const rightDomain = rightDomainProp ?? niceDomain(rightVals.length ? rightVals : leftVals);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth;
    const h = height;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const plotW = w - PAD.left - PAD.right;
    const plotH = h - PAD.top - PAD.bottom;
    const [t0, t1] = xDomain;
    const xOf = (t: number) => PAD.left + ((t - t0) / (t1 - t0 || 1)) * plotW;
    const yOf = (v: number, domain: [number, number]) =>
      PAD.top + (1 - (v - domain[0]) / (domain[1] - domain[0] || 1)) * plotH;

    // grid + axis labels
    ctx.strokeStyle = "rgba(148,163,184,0.15)";
    ctx.fillStyle = "rgba(148,163,184,0.7)";
    ctx.font = "10px ui-monospace, monospace";
    ctx.lineWidth = 1;
    const gridLines = 5;
    for (let i = 0; i <= gridLines; i++) {
      const y = PAD.top + (plotH * i) / gridLines;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(w - PAD.right, y);
      ctx.stroke();
      const lv = leftDomain[1] - ((leftDomain[1] - leftDomain[0]) * i) / gridLines;
      ctx.textAlign = "right";
      ctx.fillText(leftAxisFormat(lv), PAD.left - 6, y + 3);
      const rv = rightDomain[1] - ((rightDomain[1] - rightDomain[0]) * i) / gridLines;
      ctx.textAlign = "left";
      ctx.fillText(rightAxisFormat(rv), w - PAD.right + 6, y + 3);
    }
    // x-axis time labels
    const xTicks = 6;
    ctx.textAlign = "center";
    for (let i = 0; i <= xTicks; i++) {
      const t = t0 + ((t1 - t0) * i) / xTicks;
      ctx.fillText(fmtTime(t), xOf(t), h - 6);
    }

    // vertical reference lines (e.g. 09:16)
    for (const ref of vRefLines) {
      if (ref.t < t0 || ref.t > t1) continue;
      const x = xOf(ref.t);
      ctx.strokeStyle = ref.color ?? "rgba(148,163,184,0.6)";
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x, PAD.top);
      ctx.lineTo(x, h - PAD.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      if (ref.label) {
        ctx.fillStyle = ref.color ?? "rgba(148,163,184,0.9)";
        ctx.textAlign = "left";
        ctx.fillText(ref.label, x + 3, PAD.top + 10);
      }
    }

    // user-drawn H/V lines
    ctx.strokeStyle = "rgba(250,204,21,0.8)";
    ctx.setLineDash([2, 2]);
    for (const v of hLines) {
      const y = yOf(v, leftDomain);
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(w - PAD.right, y);
      ctx.stroke();
    }
    for (const t of vLines) {
      if (t < t0 || t > t1) continue;
      const x = xOf(t);
      ctx.beginPath();
      ctx.moveTo(x, PAD.top);
      ctx.lineTo(x, h - PAD.bottom);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // series
    for (const s of series) {
      const domain = s.axis === "left" ? leftDomain : rightDomain;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width ?? 1.5;
      ctx.setLineDash(s.dash ?? []);
      ctx.beginPath();
      let started = false;
      let prevT: number | null = null;
      for (const p of s.data) {
        // A gap much wider than one candle (e.g. the overnight/weekend
        // break between two trading days in a multi-day chart) must never
        // be bridged with a straight line -- that would visually imply
        // continuous data where there is none (spec: never pretend one
        // continuous session spans multiple days).
        const isGap = prevT != null && p.t - prevT > GAP_BREAK_MS;
        prevT = p.t;
        if (p.t < t0 || p.t > t1) continue;
        const x = xOf(p.t);
        const y = yOf(p.v, domain);
        if (!started || isGap) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // crosshair
    if (hoverT != null && hoverT >= t0 && hoverT <= t1) {
      const x = xOf(hoverT);
      ctx.strokeStyle = "rgba(226,232,240,0.5)";
      ctx.beginPath();
      ctx.moveTo(x, PAD.top);
      ctx.lineTo(x, h - PAD.bottom);
      ctx.stroke();
      for (const s of series) {
        const nearest = nearestPoint(s.data, hoverT);
        if (!nearest) continue;
        const domain = s.axis === "left" ? leftDomain : rightDomain;
        const y = yOf(nearest.v, domain);
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }, [series, xDomain, hoverT, vRefLines, hLines, vLines, leftDomain, rightDomain, height, leftAxisFormat, rightAxisFormat]);

  useEffect(() => {
    draw();
    const wrap = wrapRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [draw]);

  const tFromClientX = (clientX: number): number => {
    const canvas = canvasRef.current;
    if (!canvas) return xDomain[0];
    const rect = canvas.getBoundingClientRect();
    const w = rect.width;
    const plotW = w - PAD.left - PAD.right;
    const frac = (clientX - rect.left - PAD.left) / (plotW || 1);
    return xDomain[0] + frac * (xDomain[1] - xDomain[0]);
  };
  const vFromClientY = (clientY: number, axis: "left" | "right"): number => {
    const canvas = canvasRef.current;
    if (!canvas) return 0;
    const rect = canvas.getBoundingClientRect();
    const plotH = rect.height - PAD.top - PAD.bottom;
    const frac = 1 - (clientY - rect.top - PAD.top) / (plotH || 1);
    const domain = axis === "left" ? leftDomain : rightDomain;
    return domain[0] + frac * (domain[1] - domain[0]);
  };

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    const t = tFromClientX(e.clientX);
    const [t0, t1] = xDomain;
    const scale = e.deltaY > 0 ? 1.15 : 1 / 1.15;
    const span = (t1 - t0) * scale;
    const [f0, f1] = fullDomain;
    let nt0 = t - ((t - t0) / (t1 - t0)) * span;
    let nt1 = nt0 + span;
    if (nt1 - nt0 > f1 - f0) {
      nt0 = f0;
      nt1 = f1;
    }
    onXDomainChange([Math.max(f0, nt0), Math.min(f1, nt1)]);
  };

  const onMouseDown = (e: React.MouseEvent) => {
    if (drawMode !== "none") return;
    dragRef.current = { startX: e.clientX, startDomain: xDomain };
  };

  const onMouseMove = (e: React.MouseEvent) => {
    const t = tFromClientX(e.clientX);
    onHoverChange(t);
    if (dragRef.current) {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const plotW = rect.width - PAD.left - PAD.right;
      const dt = ((e.clientX - dragRef.current.startX) / (plotW || 1)) * (xDomain[1] - xDomain[0]);
      const [f0, f1] = fullDomain;
      const [s0, s1] = dragRef.current.startDomain;
      let nt0 = s0 - dt;
      let nt1 = s1 - dt;
      if (nt0 < f0) {
        nt1 += f0 - nt0;
        nt0 = f0;
      }
      if (nt1 > f1) {
        nt0 -= nt1 - f1;
        nt1 = f1;
      }
      onXDomainChange([nt0, nt1]);
    }
  };

  const onMouseUp = () => {
    dragRef.current = null;
  };

  const onMouseLeave = () => {
    dragRef.current = null;
    onHoverChange(null);
  };

  const onClick = (e: React.MouseEvent) => {
    if (drawMode === "hline" && onAddHLine) {
      onAddHLine(vFromClientY(e.clientY, "left"));
    } else if (drawMode === "vline" && onAddVLine) {
      onAddVLine(tFromClientX(e.clientX));
    }
  };

  return (
    <div ref={wrapRef} style={{ width: "100%", cursor: drawMode !== "none" ? "crosshair" : "grab" }}>
      <canvas
        ref={canvasRef}
        onWheel={onWheel}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseLeave}
        onClick={onClick}
      />
    </div>
  );
}

function nearestPoint(data: ChartPoint[], t: number): ChartPoint | null {
  if (data.length === 0) return null;
  let best = data[0];
  let bestDiff = Math.abs(best.t - t);
  for (const p of data) {
    const diff = Math.abs(p.t - t);
    if (diff < bestDiff) {
      best = p;
      bestDiff = diff;
    }
  }
  return best;
}
