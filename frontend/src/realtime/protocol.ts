// Wire protocol for /api/ws — mirrors trading/api/realtime/events.py.

export const MONITORING_EVENT_TYPES = [
  "strategy_status",
  "heartbeat",
  "pnl",
  "position",
  "trade",
  "server_health",
  "command",
  "alert",
  "market_quote",
  "market_status",
] as const;
export type MonitoringEventType = (typeof MONITORING_EVENT_TYPES)[number];

export interface MonitoringEvent<T = Record<string, unknown>> {
  type: MonitoringEventType;
  seq: number;
  ts: string;
  data: T;
}

export interface HelloFrame {
  type: "hello";
  server_time: string;
  seq: number;
  ping_interval: number;
  client_timeout: number;
  user: { id: number; username: string };
  event_types: MonitoringEventType[];
}

export type ServerFrame =
  | HelloFrame
  | { type: "ping"; ts: string }
  | { type: "pong"; ts: string }
  | { type: "error"; code: string; message: string }
  | { type: "subscribed"; types: MonitoringEventType[] }
  | MonitoringEvent;

export const SUBPROTOCOL = "cas.realtime.v1";

// event payload shapes we actually read in the cache-sync layer
export interface HeartbeatData {
  algo_id: string;
  server_id: string;
  status: string;
  pnl: number | null;
  timestamp: string | null;
}
export interface StrategyStatusData {
  algo_id: string;
  server_id: string;
  status: string;
  previous_status: string | null;
  source: string;
}
export interface TradeData {
  algo_id: string;
  server_id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  executed_at: string | null;
  order_id: string | null;
}
export interface PositionData {
  algo_id: string;
  server_id: string;
  symbol: string;
  quantity: number;
  average_price: number | null;
  last_price: number | null;
  pnl: number | null;
  closed: boolean;
}
export interface PnlData {
  algo_id: string;
  server_id: string;
  date: string;
  pnl: number;
  trade_count: number;
}
export interface ServerHealthData {
  server_id: string;
  status: string;
  ssm_status: string | null;
  healthy: boolean | null;
  last_heartbeat: string | null;
  source: string;
}
export interface CommandData {
  command_id: number | null;
  algo_id: string | null;
  server_id: string;
  action: string;
  status: string;
  job_id: string | null;
  requested_by: string | null;
  message: string | null;
}
export interface AlertData {
  kind: string;
  severity: "info" | "warning" | "critical" | string;
  message: string;
  algo_id: string | null;
  server_id: string | null;
  detail: Record<string, unknown>;
}

// Stage 19 market-data engine
export interface MarketQuoteData {
  symbol: string;
  kind: "index" | "option" | string;
  ltp: number | null;
  change: number | null;
  change_percent: number | null;
  timestamp: string | null;
}
export interface MarketStatusData {
  feed_state: string;
  session_state: string;
  reconnect_count?: number;
}
