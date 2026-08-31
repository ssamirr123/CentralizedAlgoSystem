// Mirrors trading/api/schemas.py (control-center /api/*) and
// trading/api/legacy.py (GET /strategies). Keep in sync with the backend —
// these are the response shapes, not a redefinition of its contract.

import type { Permission } from "@/lib/config";

// --- auth -------------------------------------------------------------
export interface AuthUser {
  id: number;
  username: string;
  email: string | null;
  role: string;
  permissions: Permission[];
  must_change_password: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: AuthUser;
}

export interface AdminUser {
  id: number;
  username: string;
  email: string | null;
  role: string;
  extra_permissions: string[];
  effective_permissions: Permission[];
  is_active: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface AuditEntry {
  id: number;
  timestamp: string;
  actor: string;
  actor_label: string | null;
  action: string;
  target: string | null;
  outcome: string;
  ip: string | null;
  detail: Record<string, unknown> | null;
}

export interface HealthResponse {
  status: "ok" | "degraded" | string;
  service: string;
  timestamp: string;
  database: string; // "connected" | "error: <ClassName>"
}

export interface ServerListEntry {
  server_id: string;
  ec2_instance_id: string;
  region: string;
  status: string;
  os: string;
  repo_path: string;
  provisioning_status: string;
  provisioning_message: string | null;
  last_heartbeat: string | null;
}

export interface ServerStatusResponse {
  name: string;
  ec2_instance_id: string;
  region: string;
  status: string;
  last_heartbeat: string | null;
  ssm_status: string | null;
  live_check_healthy: boolean | null;
}

export interface AlgoListEntry {
  algo_id: string;
  server_id: string;
  status: string;
  enabled: boolean;
  script_path: string;
  updated_at: string;
  last_heartbeat: string | null;
}

export interface AlgoStatusResponse {
  success: boolean;
  algo_id: string;
  status: string;
  pid: number | null;
  started_at: string | null;
  message: string | null;
}

export interface CommandResponse {
  success: boolean;
  command_id: number | null;
  job_id: string | null;
  status: string;
  message: string | null;
}

export interface LogEntry {
  timestamp: string;
  level: string;
  event: string;
  details: Record<string, unknown> | null;
}

export interface DailyPnlEntry {
  date: string;
  pnl: number;
  trade_count: number;
}

export interface PositionEntry {
  symbol: string;
  quantity: number;
  average_price: number;
  last_price: number | null;
  pnl: number | null;
  updated_at: string;
}

export interface TradeEntry {
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  executed_at: string;
  order_id: string | null;
}

/** Legacy GET /strategies row — richer heartbeat snapshot (MTM, day P&L). */
export interface StrategyHeartbeatOut {
  strategy_name: string;
  server_name: string;
  status: "RUNNING" | "STOPPED" | "ERROR" | string;
  current_mtm: number;
  day_pnl: number;
  number_of_trades: number;
  last_update_time: string;
  received_at: string;
}

export type AlgoAction = "start" | "stop" | "restart" | "update";

export interface AlgoActionRequest {
  algo_id: string;
  server_id: string;
  requested_by?: string | null;
}
