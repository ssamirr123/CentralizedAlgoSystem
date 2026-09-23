import { useQuery } from "@tanstack/react-query";
import * as api from "./options";

export const useOptionsExpiries = (underlying: string) =>
  useQuery({
    queryKey: ["options", "expiries", underlying],
    queryFn: () => api.getOptionsExpiries(underlying),
    enabled: !!underlying,
  });

export const useOptionsIntelligence = (
  underlying: string, expiry: string, strikeWindow: number | undefined, asOf: string | undefined,
) =>
  useQuery({
    queryKey: ["options", "intelligence", underlying, expiry, strikeWindow, asOf],
    queryFn: () => api.getOptionsIntelligence(underlying, expiry, strikeWindow, asOf),
    enabled: !!underlying && !!expiry,
  });
