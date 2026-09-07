import type { StraddleCandle } from "@/api/types";
import type { ChartPoint } from "./chart/ChartCanvas";
import { parseApiTimestampMs } from "./format";

export interface StraddlePoint extends ChartPoint {
  spot: number;
  ce: number;
  pe: number;
  volume: number | null;
}

/** Builds the combined per-minute series (spot, CE close, PE close,
 * straddle = CE + PE) driven off the spot candle timeline -- the locked
 * ATM contracts for the day, carried forward when a minute has no fresh
 * option candle (thin option liquidity). */
export function buildStraddleSeries(
  spot: StraddleCandle[], ce: StraddleCandle[], pe: StraddleCandle[],
): StraddlePoint[] {
  const ceByT = new Map(ce.map((c) => [c.timestamp, c]));
  const peByT = new Map(pe.map((c) => [c.timestamp, c]));
  let lastCe: number | null = null;
  let lastPe: number | null = null;
  const out: StraddlePoint[] = [];
  for (const s of spot) {
    const t = parseApiTimestampMs(s.timestamp);
    const cRow = ceByT.get(s.timestamp);
    const pRow = peByT.get(s.timestamp);
    if (cRow) lastCe = cRow.close;
    if (pRow) lastPe = pRow.close;
    if (lastCe == null || lastPe == null) continue; // no ATM contract data yet
    const volume = (cRow?.volume ?? 0) + (pRow?.volume ?? 0);
    out.push({ t, v: lastCe + lastPe, spot: s.close, ce: lastCe, pe: lastPe, volume: volume || null });
  }
  return out;
}
