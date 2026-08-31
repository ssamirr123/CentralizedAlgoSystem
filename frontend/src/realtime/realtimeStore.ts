// Small framework-free store for realtime connection status + a bounded
// in-memory alerts feed. Read via useSyncExternalStore.

import type { RealtimeStatus } from "./client";
import type { AlertData } from "./protocol";

export interface AlertItem extends AlertData {
  seq: number;
  ts: string;
}

interface State {
  status: RealtimeStatus;
  /** true only when the socket is fully up (hello received). */
  connected: boolean;
  lastEventAt: string | null;
  attempts: number;
  alerts: AlertItem[];
  unreadAlerts: number;
}

const ALERTS_MAX = 100;

let state: State = {
  status: "idle",
  connected: false,
  lastEventAt: null,
  attempts: 0,
  alerts: [],
  unreadAlerts: 0,
};

const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export const realtimeStore = {
  get: (): State => state,
  subscribe(fn: () => void) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  setStatus(status: RealtimeStatus, attempts: number) {
    state = { ...state, status, attempts, connected: status === "open" };
    emit();
  },
  noteEvent(ts: string) {
    state = { ...state, lastEventAt: ts };
    emit();
  },
  pushAlert(a: AlertItem) {
    state = {
      ...state,
      alerts: [a, ...state.alerts].slice(0, ALERTS_MAX),
      unreadAlerts: state.unreadAlerts + 1,
      lastEventAt: a.ts,
    };
    emit();
  },
  markAlertsRead() {
    if (state.unreadAlerts === 0) return;
    state = { ...state, unreadAlerts: 0 };
    emit();
  },
};
