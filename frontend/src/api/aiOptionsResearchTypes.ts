// Mirrors trading/ai_options_research/schemas.py exactly.

export type StrategyUniversePreset = "ALL_SUPPORTED" | "DEFINED_RISK_ONLY" | "CUSTOM";
export type ResearchJobStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";

export const STRATEGY_TYPES = [
  "BULL_PUT_CREDIT_SPREAD", "BEAR_CALL_CREDIT_SPREAD", "IRON_CONDOR", "IRON_FLY",
  "SHORT_STRADDLE", "HEDGED_SHORT_STRADDLE", "SHORT_STRANGLE", "HEDGED_SHORT_STRANGLE",
  "LONG_CALL", "LONG_PUT", "BULL_CALL_DEBIT_SPREAD", "BEAR_PUT_DEBIT_SPREAD",
  "LONG_STRADDLE", "LONG_STRANGLE", "CALENDAR_SPREAD", "DOUBLE_CALENDAR",
] as const;
export type StrategyType = (typeof STRATEGY_TYPES)[number];

export interface OptionsResearchRequest {
  underlying: string;
  expiry?: string;
  as_of?: string | null;
  strategy_universe_preset?: StrategyUniversePreset;
  strategy_universe?: StrategyType[] | null;
  candidate_limit?: number;
  debate_rounds?: number;
  llm_provider?: string | null;
  quick_think_llm?: string | null;
  deep_think_llm?: string | null;
  temperature?: number | null;
  strike_window?: number;
  min_delta?: number;
  max_delta?: number;
  minimum_volume?: number;
  minimum_oi?: number;
  maximum_bid_ask_spread?: number | null;
  wing_width?: number;
  hedge_wing_width?: number;
}

export interface SnapshotPreview {
  underlying: string;
  spot: number | null;
  expiry: string | null;
  atm_strike: number | null;
  dte_calendar: number | null;
  dte_trading: number | null;
  oi_pcr: number | null;
  max_pain_strike: number | null;
  atm_iv: number | null;
  expected_move: number | null;
  oi_support: number[];
  oi_resistance: number[];
  data_quality: string;
  data_quality_reasons: string[];
  evidence_quality: string;
  evidence_quality_reason: string;
}

export interface LegOut {
  side: string;
  option_type: string;
  strike: number;
  price: number | null;
  price_source: string;
  delta: number | null;
  iv: number | null;
  volume: number | null;
  open_interest: number | null;
  bid: number | null;
  ask: number | null;
  liquidity_ok: boolean;
  liquidity_reasons: string[];
}

export interface PayoffOut {
  net_premium: number | null;
  max_profit: number | null;
  max_profit_unbounded: boolean;
  max_loss: number | null;
  max_loss_unbounded: boolean;
  breakevens: number[];
  pricing_method: string;
  priced: boolean;
  payoff_curve: { underlying_price: number; pnl: number }[];
}

export interface CandidateOut {
  strategy: string;
  supported: boolean;
  legs: LegOut[];
  payoff: PayoffOut | null;
  liquidity_ok: boolean;
  generation_notes: string[];
  unsupported_reason: string | null;
  commentary: {
    fits_reasoning: string; invalidation: string; market_assumptions: string;
    volatility_assumptions: string; key_levels: number[];
  } | null;
  risk_analysis: {
    directional_exposure: string; volatility_exposure: string; theta_exposure: string;
    gamma_risk: string; gap_risk: string; expiry_risk: string; liquidity_risk: string;
    assignment_considerations: string; event_risk: string; tail_risk: string;
  } | null;
}

export interface ResearchReport {
  market_overview: string;
  market_regime: { trend?: string; volatility_regime?: string; market_structure?: string; evidence?: string; confidence?: string; limitations?: string };
  technical_context: string;
  news_sentiment: string;
  options_market_structure: { summary?: string; reason?: string };
  volatility_analysis: { interpretation?: string; notable_observations?: string[]; confidence?: string };
  oi_positioning_analysis: { interpretation?: string; notable_observations?: string[]; confidence?: string };
  bull_case: { thesis?: string; supporting_evidence?: string[]; key_levels?: number[] };
  bear_case: { thesis?: string; supporting_evidence?: string[]; key_levels?: number[] };
  candidates: CandidateOut[];
  invalidation_conditions: string[];
  final_summary: string;
  sanitizer_warnings: string[];
}

export interface ResearchStatus {
  research_id: string;
  underlying: string;
  status: ResearchJobStatus;
  current_stage: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error?: string | null;
  provider_error?: string | null;
  research_snapshot_id: string | null;
  evidence_quality: string | null;
}

export interface ResearchResult extends ResearchStatus {
  report: ResearchReport | null;
}

export interface StageOut {
  name: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface StagesOut {
  stages: StageOut[];
  current_stage: string | null;
}

export interface HistoryItem {
  research_id: string;
  underlying: string;
  expiry: string | null;
  status: string;
  market_regime_trend: string | null;
  candidate_count: number;
  created_at: string;
}

export interface HistoryResponse {
  items: HistoryItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface ResearchDisabled {
  disabled: true;
  reason: string;
}

export function isDisabled(payload: unknown): payload is ResearchDisabled {
  return !!payload && typeof payload === "object" && (payload as ResearchDisabled).disabled === true;
}
