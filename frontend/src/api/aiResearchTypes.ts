// Mirrors trading/ai_research/schemas.py exactly. No `any` for core research
// types — every field here corresponds to a real backend response field, not
// an invented one.

export type ResearchDepth = "quick" | "standard" | "deep";
export type ResearchJobStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
export type AnalystKey = "market" | "social" | "news" | "fundamentals";
export type Market = "US" | "INDIA";

/** Coarse /status summary bucket (grouping real graph nodes). */
export type ResearchStage =
  | "QUEUED"
  | "ANALYSTS"
  | "RESEARCH_DEBATE"
  | "TRADER"
  | "RISK"
  | "PORTFOLIO_MANAGER"
  | "COMPLETED"
  | "FAILED";

/** Per-real-node status. RUNNING is INFERRED by the backend (the next
 * not-yet-completed node after the most recently completed one) --
 * LangGraph reports node completions, not a separate "started" event. See
 * trading_agents_adapter.py's module docstring. Never render this as more
 * precise than that. */
export type NodeStageStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "SKIPPED";

export interface NodeStage {
  name: string; // ACTUAL upstream graph node name (e.g. "Market Analyst")
  status: NodeStageStatus;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
}

export interface PortfolioPositionIn {
  ticker: string;
  quantity: number;
  average_price?: number | null;
}

export interface PortfolioContextIn {
  cash?: number | null;
  currency?: string | null;
  positions: PortfolioPositionIn[];
}

/** POST /api/ai-research body. */
export interface ResearchRequest {
  symbol: string;
  research_date: string; // YYYY-MM-DD
  market?: Market; // defaults to "US" server-side when omitted
  research_depth?: ResearchDepth;
  selected_analysts?: AnalystKey[] | null;
  llm_provider?: string | null;
  deep_think_llm?: string | null;
  quick_think_llm?: string | null;
  temperature?: number | null;
  debate_rounds?: number | null;
  risk_debate_rounds?: number | null;
  retry_count?: number | null;
  data_vendors?: Record<string, string> | null;
  checkpoint_enabled?: boolean;
  portfolio?: PortfolioContextIn | null;
}

export interface ResearchSubmitted {
  research_id: string;
  status: ResearchJobStatus;
  created_at: string;
}

export interface ResearchStatus {
  research_id: string;
  symbol: string;
  research_date: string;
  research_depth: ResearchDepth;
  status: ResearchJobStatus;
  stage: ResearchStage;
  current_stage: string | null; // real node name
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
  provider_error: string | null;
  market: Market;
  exchange: string;
  currency: string;
  market_data_provenance: MarketDataProvenance | null;
}

/** Section 19/29: a small summary of the market-price data used for this
 * run's technical context -- never the candles themselves. */
export interface MarketDataProvenance {
  status: string; // AVAILABLE | PARTIAL | MISSING | STALE | PROVIDER_ERROR | RATE_LIMITED | UNSUPPORTED
  source: string; // TIMESCALEDB | ICICI_BREEZE | MIXED | NONE
  provider: string;
  symbol: string;
  interval: string;
  from: string;
  to: string;
  cutoff: string | null;
  candle_count: number;
}

/** Normalized TradingAgents output -- fields are null, never fabricated,
 * when the backend/upstream didn't populate them for this run. */
export interface ResearchReport {
  market_analysis: string | null;
  news_analysis: string | null;
  sentiment_analysis: string | null;
  fundamentals_analysis: string | null;
  bull_case: string | null;
  bear_case: string | null;
  research_manager_decision: string | null;
  trader_plan: string | null;
  risk_aggressive: string | null;
  risk_conservative: string | null;
  risk_neutral: string | null;
  risk_judge_decision: string | null;
  final_trade_decision: string | null;
  /** One of Buy/Overweight/Hold/Underweight/Sell, or "REVIEW". Research
   * output only -- never executed. */
  signal: string | null;
}

export interface ResearchResult {
  research_id: string;
  symbol: string;
  research_date: string;
  research_depth: ResearchDepth;
  status: ResearchJobStatus;
  stage: ResearchStage;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  report: ResearchReport | null;
  error: string | null;
  provider_error: string | null;
  market: Market;
  exchange: string;
  currency: string;
  market_data_provenance: MarketDataProvenance | null;
}

export interface ResearchHistoryEntry {
  research_id: string;
  symbol: string;
  research_date: string;
  research_depth: ResearchDepth;
  status: ResearchJobStatus;
  llm_provider: string | null;
  created_at: string;
  completed_at: string | null;
  market: Market;
  currency: string;
}

/** GET /api/ai-research/history -- Phase 5: database-backed, paginated
 * (replaces Phase 2-4's unbounded flat array). */
export interface ResearchHistoryPage {
  items: ResearchHistoryEntry[];
  total: number;
  page: number;
  page_size: number;
}

/** Optional filters accepted by GET /api/ai-research/history. */
export interface ResearchHistoryFilters {
  page?: number;
  page_size?: number;
  symbol?: string;
  status?: ResearchJobStatus;
  date_from?: string; // YYYY-MM-DD
  date_to?: string; // YYYY-MM-DD
  provider?: string;
}

export interface ResearchDisabled {
  enabled: false;
  message: string;
}

export interface ResearchConfigOptions {
  enabled: boolean;
  analysts: AnalystKey[];
  llm_providers: string[];
  research_depths: ResearchDepth[];
  debate_rounds_range: [number, number];
  risk_debate_rounds_range: [number, number];
  retry_count_range: [number, number];
  data_vendors: Record<string, string[]>;
  max_concurrent: number;
  timeout_seconds: number;
}

export interface Stages {
  research_id: string;
  status: ResearchJobStatus;
  current_stage: string | null;
  stages: NodeStage[];
}

export interface MemoryEntry {
  memory_id: string;
  ticker: string;
  trade_date: string;
  rating: string | null;
  pending: boolean;
  decision: string | null;
  reflection: string | null;
}

export interface ResumeRequested {
  original_research_id: string;
  new_research_id: string;
  status: ResearchJobStatus;
  resumed_from_checkpoint: boolean;
}

export function isDisabled<T>(value: T | ResearchDisabled): value is ResearchDisabled {
  return typeof value === "object" && value !== null && (value as ResearchDisabled).enabled === false;
}
