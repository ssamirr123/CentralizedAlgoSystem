import { useQuery } from "@tanstack/react-query";
import * as api from "./market";
import type { Market } from "./aiResearchTypes";

export const useMarkets = () => useQuery({ queryKey: ["ai-research", "markets"], queryFn: api.getMarkets });

export const useInstrumentSearch = (market: Market, query: string) =>
  useQuery({
    queryKey: ["ai-research", "instruments", market, query],
    queryFn: () => api.searchInstruments(market, query),
    enabled: market === "INDIA" && query.trim().length > 0,
  });

export const useCalendar = (market: Market, date: string | undefined) =>
  useQuery({
    queryKey: ["ai-research", "calendar", market, date],
    queryFn: () => api.getCalendar(market, date),
    enabled: market === "INDIA" && !!date,
  });
