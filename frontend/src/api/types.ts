// Mirrors trading/api/schemas.py (the /api/* control-center API). These
// are the response shapes, not a redefinition of the backend's contract.

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

export interface ServerPowerResponse {
  success: boolean;
  server_id: string;
  ec2_instance_id: string;
  status: string;
  message: string | null;
  safe_stop: string | null;
  running_processes: string[] | null;
}

// Request bodies — mirror trading/api/schemas.py ServerIn/ServerUpdate/AlgoIn/AlgoUpdate.
export interface ServerCreate {
  server_id: string;
  ec2_instance_id: string;
  region: string;
  status?: string;
  os?: string;
  repo_path?: string;
  auto_provision?: boolean;
}

export interface ServerPatch {
  server_id?: string;
  ec2_instance_id?: string;
  region?: string;
  status?: string;
  os?: string;
  repo_path?: string;
}

export interface AlgoCreate {
  algo_id: string;
  server_id: string;
  script_path?: string | null;
  status?: string;
  enabled?: boolean;
}

export interface AlgoPatch {
  script_path?: string;
  status?: string;
  enabled?: boolean;
}

export interface AlgoRegisterResponse {
  algo: AlgoListEntry;
  sync_attempted: boolean;
  sync_success: boolean | null;
  sync_message: string | null;
}

export type ServerPowerAction = "start" | "stop" | "restart";

export type AlgoAction = "start" | "stop" | "restart" | "update";

export interface AlgoActionRequest {
  algo_id: string;
  server_id: string;
  requested_by?: string | null;
}

// --- Stage 19 market-data engine -----------------------------------------
export interface MarketIndexQuote {
  symbol: string;
  exchange: string;
  ltp: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  prev_close: number | null;
  change: number | null;
  change_percent: number | null;
  status: "live" | "stale" | "no_data" | string;
  provider_timestamp: string | null;
  received_at: string | null;
}

export interface MarketOptionQuote {
  ltp: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  prev_close: number | null;
  volume: number | null;
  oi: number | null;
  oi_change: number | null;
  bid: number | null;
  ask: number | null;
  iv: number | null;
  vwap: number | null;
}
export interface MarketOptionChainRow {
  strike: number;
  call: MarketOptionQuote | null;
  put: MarketOptionQuote | null;
}
export interface MarketOptionChain {
  underlying: string;
  spot: number | null;
  expiry: string;
  atm_strike: number | null;
  timestamp: string;
  strikes: MarketOptionChainRow[];
}

export interface MarketCandle {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  oi: number | null;
}

export interface MarketSessionStatus {
  provider: string;
  enabled: boolean;
  session_state: string;
  feed_state: string;
  credentials: {
    source: string;
    api_key_set: boolean;
    secret_key_set: boolean;
    session_token_set: boolean;
    session_token_fingerprint: string | null;
  };
  last_session_check: string | null;
  last_error: string | null;
}

export interface MarketHealth {
  status: string;
  provider: string;
  session: string;
  feed: string;
  timezone: string;
  start_time: string;
  stop_time: string;
  symbols: Record<string, { status: string; last_update: string | null }>;
  option_chain: {
    status: string;
    expiry: string | null;
    atm_strike: number | null;
    contracts_subscribed: number;
  };
  last_error: string | null;
}
