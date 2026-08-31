import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { ReactNode } from "react";
import { authStore } from "./authStore";
import { attemptRefresh } from "@/api/client";
import * as api from "@/api/endpoints";
import type { AuthUser } from "@/api/types";
import type { Permission } from "@/lib/config";

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  /** true until the initial silent-refresh attempt settles. */
  initializing: boolean;
  baseUrl: string;
  signIn: (username: string, password: string) => Promise<AuthUser>;
  signOut: () => Promise<void>;
  refreshMe: () => Promise<void>;
  hasPermission: (perm: Permission) => boolean;
  hasAny: (perms: Permission[]) => boolean;
  setBaseUrl: (url: string) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const state = useSyncExternalStore(authStore.subscribe, authStore.get, authStore.get);
  const [initializing, setInitializing] = useState(state.sessionHint);
  const didInit = useRef(false);

  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;
    if (!authStore.get().sessionHint) {
      setInitializing(false);
      return;
    }
    attemptRefresh().finally(() => setInitializing(false));
  }, []);

  const signIn = useCallback(async (username: string, password: string) => {
    const res = await api.login(username, password);
    authStore.setSession(res.access_token, res.user);
    return res.user;
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* best effort */
    }
    authStore.clear();
  }, []);

  const refreshMe = useCallback(async () => {
    const me = await api.getMe();
    authStore.updateUser(me);
  }, []);

  const permSet = useMemo(() => new Set(state.user?.permissions ?? []), [state.user]);
  const hasPermission = useCallback((p: Permission) => permSet.has(p), [permSet]);
  const hasAny = useCallback((ps: Permission[]) => ps.some((p) => permSet.has(p)), [permSet]);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: state.user,
      isAuthenticated: !!state.accessToken && !!state.user,
      initializing,
      baseUrl: state.baseUrl,
      signIn,
      signOut,
      refreshMe,
      hasPermission,
      hasAny,
      setBaseUrl: authStore.setBaseUrl,
    }),
    [state, initializing, signIn, signOut, refreshMe, hasPermission, hasAny],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
