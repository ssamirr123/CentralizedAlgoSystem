// Mirrors trading/ai_research/backtest/schemas.py exactly.
import type { AnalystKey, Market, ResearchDepth } from "./aiResearchTypes";

export type BacktestFrequency = "daily" | "weekly" | "monthly";
export type BacktestJobStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
export type BacktestCellStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";

export interface BacktestRequest {
  symbols: string[];
  start_date: string; // YYYY-MM-DD
  end_date: string;
  frequency?: BacktestFrequency;
  market?: Market; // defaults to "US" server-side when omitted
  selected_analysts?: AnalystKey[] | null;
  research_depth?: ResearchDepth;
  llm_provider?: string | null;
  deep_think_llm?: string | null;
  quick_think_llm?: string | null;
  debate_rounds?: number | null;
  risk_debate_rounds?: number | null;
  retry_count?: number | null;
  data_vendors?: Record<string, string> | null;
  holding_days?: number | null;
}

export interface BacktestLimits {
  max_symbols: number;
  max_dates: number;
  max_runs: number;
  max_concurrent: number;
  default_holding_days: number;
}

/** Section 24: a run-count estimate only, never a monetary cost. */
export interface BacktestEstimate {
  total_runs: number;
  symbol_count: number;
  date_count: number;
  dates: string[];
  exceeds_limit: boolean;
  limit_message: string | null;
  limits: BacktestLimits;
}

export interface BacktestSubmitted {
  backtest_id: string;
  status: BacktestJobStatus;
  total_runs: number;
  created_at: string;
}

/** Section 9: progress derived only from real completed/failed run counts. */
export interface BacktestStatus {
  backtest_id: string;
  status: BacktestJobStatus;
  total_runs: number;
  completed_runs: number;
  failed_runs: number;
  remaining_runs: number;
  progress: number; // 0..1, honest (completed+failed)/total
  current_symbol: string | null;
  current_date: string | null;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

export interface BacktestCell {
  symbol: string;
  cell_date: string;
  status: BacktestCellStatus;
  research_id: string | null;
  raw_decision: string | null;
  normalized_decision: string | null;
  evaluation_status: string | null;
  raw_return: number | null;
  alpha_return: number | null;
  benchmark: string | null;
  holding_days: number | null;
  resolution_date: string | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface BacktestRunsPage {
  items: BacktestCell[];
  total: number;
  page: number;
  page_size: number;
}

export interface DirectionalAccuracy {
  count: number;
  hit_rate: number | null;
  mean_alpha: number;
}

/** Section 15/16: "AI Research Decision Evaluation" -- never a portfolio
 * P&L/Sharpe/CAGR/drawdown figure. */
export interface BacktestMetrics {
  label: string;
  total_decisions: number;
  counts_by_decision: Record<string, number>;
  resolved: number;
  pending_evaluation: number;
  unscored: number;
  by_decision: Record<string, DirectionalAccuracy>;
  overall_mean_alpha: number | null;
  overall_median_alpha: number | null;
  positive_decision_rate: number | null;
  holding_days: number;
}

export interface BacktestDetail {
  backtest_id: string;
  status: BacktestJobStatus;
  symbols: string[];
  start_date: string;
  end_date: string;
  frequency: BacktestFrequency;
  market: Market;
  research_depth: ResearchDepth;
  llm_provider: string | null;
  holding_days: number;
  total_runs: number;
  completed_runs: number;
  failed_runs: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
  metrics: BacktestMetrics | null;
}

export interface BacktestHistoryEntry {
  backtest_id: string;
  symbols: string[];
  start_date: string;
  end_date: string;
  frequency: BacktestFrequency;
  market: Market;
  status: BacktestJobStatus;
  total_runs: number;
  completed_runs: number;
  failed_runs: number;
  created_at: string;
  completed_at: string | null;
}

export interface BacktestHistoryPage {
  items: BacktestHistoryEntry[];
  total: number;
  page: number;
  page_size: number;
}

export interface BacktestResumed {
  backtest_id: string;
  status: BacktestJobStatus;
  resumed_pending_cells: number;
}

export interface BacktestCancelled {
  backtest_id: string;
  status: BacktestJobStatus;
}

/** Section 28: point-in-time safety / bias disclosure -- static per pinned
 * TradingAgents version, always shown alongside results. */
export interface DataQualitySource {
  source: string;
  safe: string; // "YES" | "NO" | "PARTIAL" | "WITHHELD"
  detail: string;
}

export interface DataQualityMatrix {
  sources: DataQualitySource[];
  summary: string;
}
