// Mirrors trading/api/options_routes.py response models exactly.

export interface ExpiryInfo {
  expiry: string;
  days_to_expiry_calendar: number;
  days_to_expiry_trading: number | null;
  is_nearest: boolean;
  is_monthly: boolean;
}

export interface OptionQuoteOut {
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  volume: number | null;
  open_interest: number | null;
  change_in_oi: number | null;
  iv: number | null;
  iv_source: string | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  moneyness: string | null;
}

export interface ChainRowOut {
  strike: number;
  call: OptionQuoteOut | null;
  put: OptionQuoteOut | null;
}

export interface ChainOut {
  underlying: string;
  expiry: string;
  spot: number | null;
  atm_strike: number | null;
  timestamp: string;
  source: string;
  provider_call_count: number;
  rows: ChainRowOut[];
}

export interface IntelligenceOut {
  underlying: string;
  spot: number | null;
  expiry: string | null;
  atm_strike: number | null;
  expiry_info: ExpiryInfo | null;
  oi_pcr: number | null;
  volume_pcr: number | null;
  total_call_oi: number | null;
  total_put_oi: number | null;
  call_oi_concentration: number | null;
  put_oi_concentration: number | null;
  oi_support: number[];
  oi_resistance: number[];
  max_pain_strike: number | null;
  max_pain_diagnostics: number[][];
  atm_iv: number | null;
  atm_iv_source: string;
  expected_move: number | null;
  expected_move_method: string;
  iv_rank: number | null;
  iv_percentile: number | null;
  iv_rank_status: string;
  iv_rank_reason: string | null;
  quality: string;
  quality_reasons: string[];
  provenance: {
    underlying_source: string;
    option_chain_source: string;
    spot_timestamp: string | null;
    chain_timestamp: string | null;
    calculation_version: string;
    risk_free_rate: number;
    provider_call_count: number;
  };
  chain: ChainOut;
  as_of: string | null;
}

export const OPTIONS_UNDERLYINGS = ["NIFTY", "SENSEX"] as const;
export type OptionsUnderlying = (typeof OPTIONS_UNDERLYINGS)[number];
