// Build-time configuration, read once. See .env.example for each var.

export type TradingMode = "paper" | "live";

/** Fail-safe: anything that is not exactly "live" is treated as paper. */
export const TRADING_MODE: TradingMode =
  (import.meta.env.VITE_TRADING_MODE ?? "").trim().toLowerCase() === "live"
    ? "live"
    : "paper";

export const IS_LIVE = TRADING_MODE === "live";

/**
 * This foundation never executes live orders. The flag is here so screens
 * can render the distinction and disable controls that would need it; it is
 * intentionally hard-wired to false and not driven by env.
 */
export const LIVE_EXECUTION_ENABLED = false;

/** Empty string => call relative `/api` (dev proxy / same-origin nginx). */
export const CONFIGURED_API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

export const DAY_LOSS_LIMIT = Number(import.meta.env.VITE_DAY_LOSS_LIMIT ?? "10000") || 10000;

export const STALE_MINUTES = Number(import.meta.env.VITE_STALE_MINUTES ?? "2") || 2;
export const STALE_MS = STALE_MINUTES * 60 * 1000;

/** Default polling cadence for live views, ms. */
export const POLL_INTERVAL_MS = 15000;

/** Backend RBAC permissions (mirror trading/api/security/permissions.py). */
export const PERMISSIONS = [
  "VIEW",
  "START",
  "STOP",
  "RESTART",
  "TRADING_CONTROL",
  "ADMIN",
] as const;
export type Permission = (typeof PERMISSIONS)[number];
