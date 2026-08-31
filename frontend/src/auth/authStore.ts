// Framework-free auth store.
//
// The access token lives ONLY in memory (never localStorage) to limit XSS
// blast radius. Persistence of the session is the httpOnly refresh cookie
// the backend sets; on load we attempt a silent /api/auth/refresh. A tiny
// non-secret "hint" flag in localStorage lets us skip that call when the
// user has explicitly logged out.

import type { AuthUser } from "@/api/types";
import { CONFIGURED_API_BASE_URL } from "@/lib/config";

const BASE_STORAGE = "tcc.apiBaseUrl";
const HINT_STORAGE = "tcc.sessionHint";

export interface AuthState {
  accessToken: string | null;
  user: AuthUser | null;
  baseUrl: string;
  /** true if we believe a refresh cookie may exist (attempt silent refresh). */
  sessionHint: boolean;
}

function readBaseUrl(): string {
  try {
    return (localStorage.getItem(BASE_STORAGE) || CONFIGURED_API_BASE_URL).replace(/\/+$/, "");
  } catch {
    return CONFIGURED_API_BASE_URL.replace(/\/+$/, "");
  }
}

function readHint(): boolean {
  try {
    return localStorage.getItem(HINT_STORAGE) === "1";
  } catch {
    return false;
  }
}

let state: AuthState = {
  accessToken: null,
  user: null,
  baseUrl: readBaseUrl(),
  sessionHint: readHint(),
};

const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export const authStore = {
  get: (): AuthState => state,
  subscribe(fn: () => void): () => void {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  setSession(accessToken: string, user: AuthUser) {
    try {
      localStorage.setItem(HINT_STORAGE, "1");
    } catch {
      /* ignore */
    }
    state = { ...state, accessToken, user, sessionHint: true };
    emit();
  },
  updateUser(user: AuthUser) {
    state = { ...state, user };
    emit();
  },
  clear() {
    try {
      localStorage.removeItem(HINT_STORAGE);
    } catch {
      /* ignore */
    }
    state = { ...state, accessToken: null, user: null, sessionHint: false };
    emit();
  },
  setBaseUrl(url: string) {
    try {
      if (url) localStorage.setItem(BASE_STORAGE, url);
      else localStorage.removeItem(BASE_STORAGE);
    } catch {
      /* ignore */
    }
    state = { ...state, baseUrl: (url || CONFIGURED_API_BASE_URL).replace(/\/+$/, "") };
    emit();
  },
};

/** Read the non-httpOnly double-submit CSRF cookie for refresh/logout. */
export function readCsrfCookie(): string {
  const m = document.cookie.match(/(?:^|;\s*)cas_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}
