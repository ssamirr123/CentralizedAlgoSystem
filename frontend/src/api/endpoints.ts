import { apiRequest } from "./client";
import type {
  AlgoAction,
  AlgoListEntry,
  AlgoStatusResponse,
  CommandResponse,
  DailyPnlEntry,
  HealthResponse,
  LogEntry,
  PositionEntry,
  ServerListEntry,
  ServerStatusResponse,
  StrategyHeartbeatOut,
  TradeEntry,
} from "./types";

// --- health -------------------------------------------------------------
export const getHealth = () =>
  apiRequest<HealthResponse>("/api/health", { auth: false });

// --- servers ----------------------------------------------------------
export const listServers = () => apiRequest<ServerListEntry[]>("/api/servers");

export const getServerStatus = (server_id: string, live = false) =>
  apiRequest<ServerStatusResponse>("/api/server/status", { query: { server_id, live } });

// --- algos / strategies ----------------------------------------------
export const listAlgos = () => apiRequest<AlgoListEntry[]>("/api/algos");

export const getAlgoStatus = (algo_id: string, server_id: string) =>
  apiRequest<AlgoStatusResponse>("/api/algo/status", { query: { algo_id, server_id } });

// --- commands -------------------------------------------------------
export const runAlgoAction = (action: AlgoAction, algo_id: string, server_id: string, requested_by?: string) =>
  apiRequest<CommandResponse>(`/api/algo/${action}`, {
    method: "POST",
    body: { algo_id, server_id, requested_by: requested_by ?? null },
  });

export const getCommand = (command_id: number) =>
  apiRequest<CommandResponse>(`/api/command/${command_id}`);

// --- heartbeats (legacy richer snapshot) ---------------------------
export const listStrategyHeartbeats = () =>
  apiRequest<StrategyHeartbeatOut[]>("/strategies", { auth: false });

// --- pnl ----------------------------------------------------------
export const getPnlToday = (pnl_date?: string) =>
  apiRequest<Record<string, number>>("/api/pnl/today", { query: { pnl_date } });

export const getPnlHistory = (algo_id: string, server_id: string) =>
  apiRequest<DailyPnlEntry[]>("/api/pnl", { query: { algo_id, server_id } });

// --- positions --------------------------------------------------
export const getPositions = (algo_id: string, server_id: string) =>
  apiRequest<PositionEntry[]>("/api/positions", { query: { algo_id, server_id } });

// --- trades ---------------------------------------------------
export const getTrades = (algo_id: string, server_id: string, limit = 100) =>
  apiRequest<TradeEntry[]>("/api/trades", { query: { algo_id, server_id, limit } });

// --- logs -----------------------------------------------------
export interface LogQuery {
  algo_id: string;
  server_id: string;
  limit?: number;
  level?: string;
  event?: string;
  log_date?: string;
}
export const getLogs = (q: LogQuery) => apiRequest<LogEntry[]>("/api/logs", { query: { ...q } });
