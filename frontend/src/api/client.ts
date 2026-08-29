import { authStore } from "@/auth/authStore";

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
  /** Send the X-API-Key header. Default true. GET /api/health does not need it. */
  auth?: boolean;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const base = authStore.get().baseUrl; // "" => relative
  const p = path.startsWith("/") ? path : `/${path}`;
  const qs = new URLSearchParams();
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return `${base}${p}${suffix}`;
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

export async function apiRequest<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, body, auth = true, signal } = opts;
  const headers: Record<string, string> = { Accept: "application/json" };

  if (auth) {
    const key = authStore.get().apiKey;
    if (!key) throw new ApiError(401, "Not signed in — enter your API key.");
    headers["X-API-Key"] = key;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (e) {
    throw new ApiError(0, e instanceof Error ? `Network error: ${e.message}` : "Network error");
  }

  if (res.status === 204) return undefined as T;
  if (!res.ok) throw new ApiError(res.status, await parseError(res));

  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}
