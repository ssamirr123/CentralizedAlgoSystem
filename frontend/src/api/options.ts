// Options Intelligence API client -- trading/api/options_routes.py.
import { apiRequest } from "./client";
import type { ChainOut, ExpiryInfo, IntelligenceOut } from "./optionsTypes";

export const getOptionsExpiries = (underlying: string) =>
  apiRequest<ExpiryInfo[]>("/api/options/expiries", { query: { underlying } });

export const getOptionsChain = (underlying: string, expiry: string, strikeWindow?: number, asOf?: string) =>
  apiRequest<ChainOut>("/api/options/chain", {
    query: { underlying, expiry, strike_window: strikeWindow, as_of: asOf },
  });

export const getOptionsIntelligence = (underlying: string, expiry: string, strikeWindow?: number, asOf?: string) =>
  apiRequest<IntelligenceOut>("/api/options/intelligence", {
    query: { underlying, expiry, strike_window: strikeWindow, as_of: asOf },
  });
