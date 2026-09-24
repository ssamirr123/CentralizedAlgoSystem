// Mirrors trading/ai_research/market/schemas.py exactly.
import type { Market } from "./aiResearchTypes";

export type Exchange = "NSE" | "GENERIC";
export type InstrumentType = "EQUITY" | "INDEX";

export interface Instrument {
  canonical_id: string;
  market: Market;
  exchange: Exchange;
  instrument_type: InstrumentType;
  symbol: string;
  display_name: string;
  currency: string;
  timezone: string;
}

export interface MarketsResponse {
  markets: Market[];
  default: Market;
}

export interface InstrumentSearchResponse {
  items: Instrument[];
}

export interface TradingDateValidation {
  requested: string;
  is_valid: boolean;
  reason: string | null;
  previous_trading_day: string | null;
  next_trading_day: string | null;
}

export interface CalendarResponse {
  market: Market;
  timezone: string;
  covered_years: number[];
  validation: TradingDateValidation | null;
}
