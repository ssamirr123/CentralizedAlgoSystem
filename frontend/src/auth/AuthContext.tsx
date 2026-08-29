import { createContext, useCallback, useContext, useMemo, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { authStore } from "./authStore";
import { apiRequest } from "@/api/client";

interface AuthContextValue {
  apiKey: string | null;
  baseUrl: string;
  isAuthenticated: boolean;
  /** Verifies the key against an auth-protected endpoint before persisting. */
  signIn: (apiKey: string, baseUrlOverride?: string) => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const state = useSyncExternalStore(authStore.subscribe, authStore.get, authStore.get);

  const signIn = useCallback(async (apiKey: string, baseUrlOverride?: string) => {
    const key = apiKey.trim();
    if (!key) throw new Error("API key is required.");
    // Persist first so apiRequest picks up the key + base URL, then probe.
    authStore.signIn(key, baseUrlOverride?.trim());
    try {
      await apiRequest("/api/algos", { method: "GET" });
    } catch (e) {
      authStore.signOut();
      throw e;
    }
  }, []);

  const signOut = useCallback(() => authStore.signOut(), []);

  const value = useMemo<AuthContextValue>(
    () => ({
      apiKey: state.apiKey,
      baseUrl: state.baseUrl,
      isAuthenticated: !!state.apiKey,
      signIn,
      signOut,
    }),
    [state, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
