import type { ChartPoint } from "./ChartCanvas";

/** Standard exponential moving average, client-side only (not persisted). */
export function ema(points: ChartPoint[], length: number): ChartPoint[] {
  if (points.length === 0) return [];
  const k = 2 / (length + 1);
  const out: ChartPoint[] = [{ t: points[0].t, v: points[0].v }];
  for (let i = 1; i < points.length; i++) {
    const prev = out[i - 1].v;
    out.push({ t: points[i].t, v: points[i].v * k + prev * (1 - k) });
  }
  return out;
}

/** Volume-weighted average price, cumulative from the first point (session
 * start). Falls back to an unweighted running average if no volume is
 * available on any point (equal weight of 1). */
export function vwap(points: { t: number; v: number; volume: number | null }[]): ChartPoint[] {
  let cumPV = 0;
  let cumV = 0;
  const out: ChartPoint[] = [];
  for (const p of points) {
    const vol = p.volume ?? 1;
    cumPV += p.v * vol;
    cumV += vol;
    out.push({ t: p.t, v: cumV > 0 ? cumPV / cumV : p.v });
  }
  return out;
}
