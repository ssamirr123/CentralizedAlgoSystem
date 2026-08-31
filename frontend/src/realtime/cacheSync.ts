import type { QueryClient } from "@tanstack/react-query";
import type { AlgoListEntry, ServerListEntry } from "@/api/types";
import type {
  CommandData,
  HeartbeatData,
  MonitoringEvent,
  PnlData,
  ServerHealthData,
  StrategyStatusData,
} from "./protocol";

/**
 * Apply one realtime event to the react-query cache.
 *
 * High-frequency status fields are patched in place (no network). Lower-
 * frequency / structurally-tricky data (positions, trades, pnl history,
 * server list) is invalidated so react-query refetches once — that is a
 * targeted resync, not the polling fallback.
 */
export function applyEventToCache(qc: QueryClient, ev: MonitoringEvent): void {
  switch (ev.type) {
    case "heartbeat": {
      const d = ev.data as unknown as HeartbeatData;
      patchAlgo(qc, d.algo_id, d.server_id, {
        status: d.status,
        last_heartbeat: d.timestamp ?? new Date().toISOString(),
      });
      break;
    }
    case "strategy_status": {
      const d = ev.data as unknown as StrategyStatusData;
      patchAlgo(qc, d.algo_id, d.server_id, { status: d.status });
      break;
    }
    case "pnl": {
      const d = ev.data as unknown as PnlData;
      qc.setQueriesData<Record<string, number>>({ queryKey: ["pnl-today"] }, (prev) =>
        prev ? { ...prev, [`${d.algo_id}|${d.server_id}`]: d.pnl } : prev,
      );
      qc.invalidateQueries({ queryKey: ["pnl-history", d.algo_id, d.server_id] });
      break;
    }
    case "position": {
      const d = ev.data as unknown as { algo_id: string; server_id: string };
      qc.invalidateQueries({ queryKey: ["positions", d.algo_id, d.server_id] });
      break;
    }
    case "trade": {
      const d = ev.data as unknown as { algo_id: string; server_id: string };
      qc.invalidateQueries({ queryKey: ["trades", d.algo_id, d.server_id] });
      break;
    }
    case "server_health": {
      const d = ev.data as unknown as ServerHealthData;
      qc.setQueryData<ServerListEntry[]>(["servers"], (prev) =>
        prev?.map((s) => (s.server_id === d.server_id ? { ...s, status: d.status, last_heartbeat: d.last_heartbeat ?? s.last_heartbeat } : s)),
      );
      qc.invalidateQueries({ queryKey: ["server-status", d.server_id] });
      break;
    }
    case "command": {
      const d = ev.data as unknown as CommandData;
      // a command can flip an algo's status; cheapest correct action is a
      // single refetch of the algo list.
      qc.invalidateQueries({ queryKey: ["algos"] });
      if (d.algo_id) qc.invalidateQueries({ queryKey: ["algo-status", d.algo_id, d.server_id] });
      break;
    }
    // "alert" is handled by RealtimeContext -> realtimeStore, not the cache.
  }
}

function patchAlgo(qc: QueryClient, algoId: string, serverId: string, patch: Partial<AlgoListEntry>): void {
  qc.setQueryData<AlgoListEntry[]>(["algos"], (prev) =>
    prev?.map((a) => (a.algo_id === algoId && a.server_id === serverId ? { ...a, ...patch } : a)),
  );
}
