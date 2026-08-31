import { authStore, readCsrfCookie } from "@/auth/authStore";
import type { TokenResponse } from "./types";

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: Record<string, string | number | boolean | undefined | null>;
  body?: unknown;
  /** Attach the bearer access token. Default true. */
  auth?: boolean;
  /** Extra request headers (e.g. X-CSRF-Token for logout). */
  headers?: Record<string, string>;
  /** Internal: don't attempt a token refresh + retry on 401. */
  _noRetry?: boolean;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const base = authStore.get().baseUrl;
  const p = path.startsWith("/") ? path : `/${path}`;
  const qs = new URLSearchParams();
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
  }
  return `${base}${p}${qs.toString() ? `?${qs}` : ""}`;
}

async function parseError(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail)) return data.detail.map((d: { msg?: string }) => d.msg).join("; ");
    if (typeof data?.message === "string") return data.message;
    return JSON.stringify(data);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

// --- silent refresh (single-flight) ---------------------------------
let refreshInFlight: Promise<boolean> | null = null;

export async function attemptRefresh(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    try {
      const res = await fetch(buildUrl("/api/auth/refresh"), {
        method: "POST",
        headers: { "X-CSRF-Token": readCsrfCookie() },
        credentials: "include",
      });
      if (!res.ok) {
        authStore.clear();
        return false;
      }
      const data = (await res.json()) as TokenResponse;
      authStore.setSession(data.access_token, data.user);
      return true;
    } catch {
      authStore.clear();
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

async function doFetch(path: string, opts: RequestOptions): Promise<Response> {
  const { method = "GET", query, body, auth = true } = opts;
  const headers: Record<string, string> = { Accept: "application/json", ...(opts.headers ?? {}) };
  if (auth) {
    const token = authStore.get().accessToken;
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return fetch(buildUrl(path, query), {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "include",
    signal: opts.signal,
  });
}

export async function apiRequest<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  let res: Response;
  try {
    res = await doFetch(path, opts);
  } catch (e) {
    throw new ApiError(0, e instanceof Error ? `Network error: ${e.message}` : "Network error");
  }

  // One transparent refresh + retry on 401 for authed calls.
  if (
    res.status === 401 &&
    opts.auth !== false &&
    !opts._noRetry &&
    !path.startsWith("/api/auth/")
  ) {
    if (await attemptRefresh()) {
      try {
        res = await doFetch(path, opts);
      } catch (e) {
        throw new ApiError(0, e instanceof Error ? `Network error: ${e.message}` : "Network error");
      }
    }
  }

  if (res.status === 204) return undefined as T;
  if (!res.ok) throw new ApiError(res.status, await parseError(res));
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}
