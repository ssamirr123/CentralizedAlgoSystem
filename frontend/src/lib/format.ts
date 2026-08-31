import { STALE_MS } from "./config";

const IST = "Asia/Kolkata";

export function formatIST(value: string | number | Date | null | undefined): string {
  if (value == null) return "—";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return (
    new Intl.DateTimeFormat("en-IN", {
      timeZone: IST,
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(d) + " IST"
  );
}

export function relativeAge(value: string | number | Date | null | undefined): string {
  if (value == null) return "never";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  const diff = Date.now() - d.getTime();
  if (diff < 0) return "just now";
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Today's date in IST as YYYY-MM-DD (for /api/pnl/today?pnl_date=). */
export function istDateToday(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: IST }).format(new Date());
}

export function isStale(value: string | number | Date | null | undefined): boolean {
  if (value == null) return true;
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return true;
  return Date.now() - d.getTime() > STALE_MS;
}

const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

export function formatINR(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return inr.format(value);
}

export function formatNumber(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-IN").format(value);
}

export function pnlSign(value: number | null | undefined): "pos" | "neg" | "zero" {
  if (value == null || Number.isNaN(value) || value === 0) return "zero";
  return value > 0 ? "pos" : "neg";
}
