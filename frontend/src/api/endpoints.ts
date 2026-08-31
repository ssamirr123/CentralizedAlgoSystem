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
  StrategyHeartbeatOut,
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
