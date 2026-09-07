import type { QueryClient } from "@tanstack/react-query";
import type { AlgoListEntry, ServerListEntry } from "@/api/types";
import type {
  CommandData,
  HeartbeatData,
  MarketQuoteData,
  MarketStatusData,
  MonitoringEvent,
  PnlData,
  ServerHealthData,
  StrategyStatusData,
} from "./protocol";
import type { MarketIndexQuote } from "@/api/types";

// The option chain gets a market_quote event per option tick — dozens per
// second across the subscribed contracts. Invalidating the query on each
// one triggers a refetch storm that trips the API rate limit. Coalesce
// into at most one refetch per this interval (the hook also polls at 5s).
const OPTION_CHAIN_RESYNC_MS = 3000;
let optionChainResyncAt = 0;

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
    case "market_quote": {
      const d = ev.data as unknown as MarketQuoteData;
      if (d.kind === "index") {
        qc.setQueryData<MarketIndexQuote[]>(["market-indices"], (prev) =>
          prev?.map((q) =>
            q.symbol === d.symbol
              ? { ...q, ltp: d.ltp, change: d.change, change_percent: d.change_percent, status: "live" }
              : q,
          ),
        );
      } else {
        // option quotes changed -> the chain needs a resync, but throttled
        // (see OPTION_CHAIN_RESYNC_MS) so a burst of ticks is one refetch.
        const now = Date.now();
        if (now - optionChainResyncAt >= OPTION_CHAIN_RESYNC_MS) {
          optionChainResyncAt = now;
          qc.invalidateQueries({ queryKey: ["nifty-option-chain"] });
        }
      }
      break;
    }
    case "market_status": {
      const d = ev.data as unknown as MarketStatusData;
      qc.setQueryData<Record<string, unknown>>(["market-health"], (prev) =>
        prev ? { ...prev, feed: d.feed_state, session: d.session_state } : prev,
      );
      qc.invalidateQueries({ queryKey: ["market-session"] });
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
