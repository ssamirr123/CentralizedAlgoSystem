const IST = "Asia/Kolkata";

/** Backend candle/snapshot timestamps are always UTC (see
 * trading/market_data/models.py), but a SQLite dev DB round-trips
 * `DateTime(timezone=True)` as a naive string with no offset -- Postgres
 * (production) does not have this gap. `Date.parse` on an offset-less
 * string falls back to the *viewer's own* local timezone, which silently
 * shifts every chart. Always parse these as UTC explicitly. */
export function parseApiTimestampMs(ts: string): number {
  const hasOffset = /[zZ]|[+-]\d\d:?\d\d$/.test(ts);
  return Date.parse(hasOffset ? ts : `${ts}Z`);
}

/** HH:MM in IST regardless of the viewer's own timezone -- this is an
 * Indian-market dashboard, session times are always IST. */
export function formatTimeIST(epochMs: number): string {
  return new Date(epochMs).toLocaleTimeString("en-IN", {
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone: IST,
  });
}

const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

/** Epoch ms for an IST wall-clock hour:minute on a plain "YYYY-MM-DD"
 * trading date -- computed directly from the fixed +05:30 offset, never
 * via the viewer's own Date/timezone (which would silently use the
 * browser's local timezone instead of IST). */
export function istWallClockToEpoch(isoDate: string, hour: number, minute: number): number {
  const [y, m, d] = isoDate.split("-").map(Number);
  const utcMidnight = Date.UTC(y, m - 1, d);
  return utcMidnight + hour * 3600000 + minute * 60000 - IST_OFFSET_MS;
}

/** "Wed, 02 Sep" from a plain "YYYY-MM-DD" calendar date -- parsed from
 * the string directly (no Date/timezone round-trip) since a date-only
 * value has no timezone of its own. */
export function formatDateLabel(isoDate: string, opts: { withWeekday?: boolean; withMonth?: boolean } = {}): string {
  const { withWeekday = true, withMonth = true } = opts;
  const [y, m, d] = isoDate.split("-").map(Number);
  const utcNoon = new Date(Date.UTC(y, m - 1, d, 12));
  const parts: string[] = [];
  if (withWeekday) parts.push(utcNoon.toLocaleDateString("en-IN", { weekday: "short", timeZone: "UTC" }) + ",");
  parts.push(String(d).padStart(2, "0"));
  if (withMonth) parts.push(utcNoon.toLocaleDateString("en-IN", { month: "short", timeZone: "UTC" }));
  return parts.join(" ");
}
