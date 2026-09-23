// Market domain API client -- trading/ai_research/market/router.py.
import { apiRequest } from "./client";
import type { Market } from "./aiResearchTypes";
import type { CalendarResponse, InstrumentSearchResponse, MarketsResponse } from "./marketTypes";

export const getMarkets = () => apiRequest<MarketsResponse>("/api/ai-research/markets");

export const searchInstruments = (market: Market, query: string) =>
  apiRequest<InstrumentSearchResponse>("/api/ai-research/instruments", { query: { market, query } });

export const getCalendar = (market: Market, date?: string) =>
  apiRequest<CalendarResponse>("/api/ai-research/calendar", { query: { market, date } });
