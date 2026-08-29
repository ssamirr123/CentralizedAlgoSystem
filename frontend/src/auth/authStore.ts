// Tiny framework-free store for the API key + optional base-URL override,
// persisted to localStorage. The API client reads from here so it does not
// depend on React context; AuthContext is the React-facing wrapper.

import { CONFIGURED_API_BASE_URL } from "@/lib/config";

const KEY_STORAGE = "tcc.apiKey";
const BASE_STORAGE = "tcc.apiBaseUrl";

export interface AuthState {
  apiKey: string | null;
  /** Effective base URL for API calls; "" means relative same-origin. */
  baseUrl: string;
}

function read(): AuthState {
  let apiKey: string | null = null;
  let baseOverride = "";
  try {
    apiKey = localStorage.getItem(KEY_STORAGE);
    baseOverride = localStorage.getItem(BASE_STORAGE) ?? "";
  } catch {
    // localStorage unavailable (private mode / blocked) — run in-memory.
  }
  return {
    apiKey: apiKey && apiKey.length > 0 ? apiKey : null,
    baseUrl: (baseOverride || CONFIGURED_API_BASE_URL).replace(/\/+$/, ""),
  };
}

let state: AuthState = read();
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export const authStore = {
  get(): AuthState {
    return state;
  },
  subscribe(fn: () => void): () => void {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  signIn(apiKey: string, baseUrlOverride?: string) {
    try {
      localStorage.setItem(KEY_STORAGE, apiKey);
      if (baseUrlOverride !== undefined) {
        if (baseUrlOverride) localStorage.setItem(BASE_STORAGE, baseUrlOverride);
        else localStorage.removeItem(BASE_STORAGE);
      }
    } catch {
      /* ignore persistence failure */
    }
    state = read();
    // read() re-derives from storage; if storage failed, apply directly.
    if (state.apiKey !== apiKey) {
      state = {
        apiKey,
        baseUrl: (baseUrlOverride || CONFIGURED_API_BASE_URL).replace(/\/+$/, ""),
      };
    }
    emit();
  },
  signOut() {
    try {
      localStorage.removeItem(KEY_STORAGE);
    } catch {
      /* ignore */
    }
    state = { ...read(), apiKey: null };
    emit();
  },
};
