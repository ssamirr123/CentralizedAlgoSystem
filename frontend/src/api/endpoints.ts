import { apiRequest } from "./client";
import { readCsrfCookie } from "@/auth/authStore";
import type {
  AdminUser,
  AlgoAction,
  AlgoListEntry,
  AlgoStatusResponse,
  AuditEntry,
  AuthUser,
  CommandResponse,
  DailyPnlEntry,
  HealthResponse,
  LogEntry,
  PositionEntry,
  ServerListEntry,
  ServerStatusResponse,
  TokenResponse,
  TradeEntry,
} from "./types";

// --- auth -----------------------------------------------------------
export const login = (username: string, password: string) =>
  apiRequest<TokenResponse>("/api/auth/login", { method: "POST", body: { username, password }, auth: false });

export const logout = () =>
  apiRequest<void>("/api/auth/logout", {
    method: "POST",
    auth: false,
    headers: { "X-CSRF-Token": readCsrfCookie() },
  });

export const getMe = () => apiRequest<AuthUser>("/api/auth/me");

export const changePassword = (current_password: string, new_password: string) =>
  apiRequest<void>("/api/auth/change-password", {
    method: "POST",
    body: { current_password, new_password },
  });

// --- admin ---------------------------------------------------------
export const listAdminUsers = () => apiRequest<AdminUser[]>("/api/admin/users");

export const createAdminUser = (body: {
  username: string;
  password: string;
  role: string;
  email?: string | null;
}) => apiRequest<AdminUser>("/api/admin/users", { method: "POST", body });

export const updateAdminUser = (
  id: number,
  body: { role?: string; is_active?: boolean; email?: string | null; extra_permissions?: string[] },
) => apiRequest<AdminUser>(`/api/admin/users/${id}`, { method: "PATCH", body });

export const resetAdminUserPassword = (id: number, new_password: string) =>
  apiRequest<void>(`/api/admin/users/${id}/reset-password`, { method: "POST", body: { new_password } });

export const deactivateAdminUser = (id: number) =>
  apiRequest<void>(`/api/admin/users/${id}`, { method: "DELETE" });

export const getAudit = (q: { actor?: string; action?: string; outcome?: string; limit?: number } = {}) =>
  apiRequest<AuditEntry[]>("/api/admin/audit", { query: { ...q } });

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

// --- market data (Stage 19) ---------------------------------------
export const getMarketIndices = () =>
  apiRequest<import("./types").MarketIndexQuote[]>("/api/market/indices");

export const getMarketIndex = (symbol: string) =>
  apiRequest<import("./types").MarketIndexQuote>(`/api/market/indices/${encodeURIComponent(symbol)}`);

export const getMarketCandles = (symbol: string, interval = "1minute", limit = 375) =>
  apiRequest<import("./types").MarketCandle[]>(`/api/market/candles/${encodeURIComponent(symbol)}`, {
    query: { interval, limit },
  });

export const getMarketHealth = () => apiRequest<import("./types").MarketHealth>("/api/market/health");

export const getMarketSessionStatus = () =>
  apiRequest<import("./types").MarketSessionStatus>("/api/market/session/status");

export const updateMarketSession = (body: { session_token: string; api_key?: string; secret_key?: string }) =>
  apiRequest<import("./types").MarketSessionStatus>("/api/market/session", { method: "POST", body });

// --- Straddle Pulse (NIFTY + SENSEX expiry-cycle model) --------------------
export const getStraddleUnderlyings = () =>
  apiRequest<import("./types").StraddleUnderlying[]>("/api/market/straddle-pulse/underlyings");

export const getStraddleCycles = (underlying: string) =>
  apiRequest<import("./types").StraddleCycle[]>("/api/market/straddle-pulse/cycles", { query: { underlying } });

export const getCycleSessions = (cycleId: number) =>
  apiRequest<import("./types").StraddleSession[]>(`/api/market/straddle-pulse/cycles/${cycleId}/sessions`);

export const getSessionDetail = (sessionId: number) =>
  apiRequest<import("./types").StraddleSession>(`/api/market/straddle-pulse/sessions/${sessionId}`);

export const getSessionChart = (sessionId: number) =>
  apiRequest<import("./types").StraddleSessionChart>(`/api/market/straddle-pulse/sessions/${sessionId}/chart`);

export const getSessionOI = (sessionId: number) =>
  apiRequest<import("./types").StraddleSessionOI>(`/api/market/straddle-pulse/sessions/${sessionId}/oi`);

// --- Trading Control Center execution framework (Phase 11/12) --------
// trading/api/execution_routes.py -- the broker-agnostic execution
// framework's own API, distinct from the legacy algos/positions/pnl/logs
// endpoints above. See api/types.ts's matching comment.
export const listExecutionStrategies = () =>
  apiRequest<import("./types").ExecutionStrategy[]>("/api/strategies");

export const getExecutionStrategy = (strategyId: string) =>
  apiRequest<import("./types").ExecutionStrategy>(`/api/strategies/${encodeURIComponent(strategyId)}`);

export const startExecutionStrategy = (strategyId: string) =>
  apiRequest<import("./types").ExecutionStrategy>(`/api/strategies/${encodeURIComponent(strategyId)}/start`, {
    method: "POST",
  });

export const stopExecutionStrategy = (strategyId: string) =>
  apiRequest<import("./types").ExecutionStrategy>(`/api/strategies/${encodeURIComponent(strategyId)}/stop`, {
    method: "POST",
  });

export const listExecutionAccounts = () =>
  apiRequest<import("./types").ExecutionAccount[]>("/api/accounts");

export const listExecutionBrokers = () =>
  apiRequest<import("./types").ExecutionBroker[]>("/api/brokers");

export const listExecutionAssignments = () =>
  apiRequest<import("./types").ExecutionAssignment[]>("/api/assignments");

export const createExecutionAssignment = (body: import("./types").ExecutionAssignmentCreate) =>
  apiRequest<import("./types").ExecutionAssignment>("/api/assignments", { method: "POST", body });

export const listExecutionModes = () =>
  apiRequest<import("./types").ExecutionModeOption[]>("/api/execution-modes");

export const getRiskStatus = () => apiRequest<import("./types").RiskStatus>("/api/risk/status");

export const getRiskLimits = () => apiRequest<import("./types").RiskLimits>("/api/risk/limits");

export const setKillSwitch = (engaged: boolean, reason?: string) =>
  apiRequest<import("./types").KillSwitchState>("/api/risk/kill-switch", {
    method: "POST",
    body: { engaged, reason: reason ?? "" },
  });

export const listExecutionOrders = () => apiRequest<import("./types").ExecutionOrder[]>("/api/execution/orders");

export const listExecutionPositions = () =>
  apiRequest<import("./types").ExecutionPosition[]>("/api/execution/positions");

export const getExecutionPnl = () => apiRequest<import("./types").ExecutionPnl>("/api/execution/pnl");

export const getExecutionSystemStatus = () =>
  apiRequest<import("./types").ExecutionSystemStatus>("/api/system/status");
