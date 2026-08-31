import { useEffect } from "react";
import type { ReactNode } from "react";
import { useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { REALTIME_ENABLED } from "@/lib/config";
import { RealtimeClient } from "./client";
import { applyEventToCache } from "./cacheSync";
import { realtimeStore } from "./realtimeStore";
import type { AlertData, MonitoringEvent } from "./protocol";

/**
 * Owns the single WebSocket connection. Mounted once, inside both the
 * auth and react-query providers. It never renders UI — status + alerts
 * are read from `realtimeStore` via `useRealtime()`.
 *
 * Graceful degradation: while the socket is not "open", every react-query
 * hook falls back to interval polling (see usePollInterval); when it
 * connects, `onHello` invalidates the active queries once (REST is the
 * source of truth) and polling stops.
 */
export function RealtimeProvider({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  const qc = useQueryClient();

  useEffect(() => {
    if (!REALTIME_ENABLED || !isAuthenticated) {
      realtimeStore.setStatus("idle", 0);
      return;
    }
    const client = new RealtimeClient({
      onStatus: (status, detail) => realtimeStore.setStatus(status, detail?.attempts ?? 0),
      onHello: () => {
        // resync the truth over REST, then rely on the stream
        qc.invalidateQueries();
      },
      onEvent: (ev: MonitoringEvent) => {
        realtimeStore.noteEvent(ev.ts);
        if (ev.type === "alert") {
          const d = ev.data as unknown as AlertData;
          realtimeStore.pushAlert({ ...d, seq: ev.seq, ts: ev.ts });
        }
        applyEventToCache(qc, ev);
      },
    });
    client.start();
    return () => client.stop();
  }, [isAuthenticated, qc]);

  return <>{children}</>;
}

export function useRealtime() {
  const state = useSyncExternalStore(realtimeStore.subscribe, realtimeStore.get, realtimeStore.get);
  return {
    ...state,
    markAlertsRead: realtimeStore.markAlertsRead,
  };
}

/** Interval for react-query `refetchInterval`: false while realtime is live. */
export function usePollInterval(fallbackMs: number): number | false {
  const { connected } = useRealtime();
  return connected ? false : fallbackMs;
}
