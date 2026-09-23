// Polling lifecycle -- same react-query refetchInterval convention as
// aiResearchHooks.ts (Section 30's own recommendation: a poll every 1-2s
// is sufficient for this workload; no new real-time transport needed).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as api from "./aiResearchBacktest";
import { isDisabled } from "./aiResearchTypes";
import type { BacktestRequest } from "./aiResearchBacktestTypes";

const POLL_MS = 2000;

export const useSubmitBacktest = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: BacktestRequest) => api.submitBacktest(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-research", "backtests", "history"] });
    },
  });
};

export const useEstimateBacktest = () => useMutation({ mutationFn: (body: BacktestRequest) => api.estimateBacktest(body) });

export const useBacktestHistory = (params: { page?: number; page_size?: number; status?: string } = {}) =>
  useQuery({
    queryKey: ["ai-research", "backtests", "history", params],
    queryFn: () => api.getBacktestHistory(params),
  });

export const useBacktestStatus = (backtestId: string | null) =>
  useQuery({
    queryKey: ["ai-research", "backtests", "status", backtestId],
    queryFn: () => api.getBacktestStatus(backtestId as string),
    enabled: !!backtestId,
    refetchInterval: (query) => {
      const d = query.state.data;
      if (!d || isDisabled(d)) return false;
      return d.status === "COMPLETED" || d.status === "FAILED" || d.status === "CANCELLED" ? false : POLL_MS;
    },
  });

export const useBacktestDetail = (backtestId: string | null, status: string | undefined) =>
  useQuery({
    queryKey: ["ai-research", "backtests", "detail", backtestId],
    queryFn: () => api.getBacktest(backtestId as string),
    enabled: !!backtestId && (status === "COMPLETED" || status === "FAILED" || status === "CANCELLED"),
  });

export const useBacktestRuns = (backtestId: string | null, activeStatus: string | undefined, page = 1) =>
  useQuery({
    queryKey: ["ai-research", "backtests", "runs", backtestId, page],
    queryFn: () => api.getBacktestRuns(backtestId as string, { page }),
    enabled: !!backtestId,
    refetchInterval: activeStatus === "RUNNING" || activeStatus === "QUEUED" ? POLL_MS : false,
  });

export const useDataQuality = () =>
  useQuery({ queryKey: ["ai-research", "backtests", "data-quality"], queryFn: api.getDataQuality });

export const useResumeBacktest = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (backtestId: string) => api.resumeBacktest(backtestId),
    onSuccess: (_data, backtestId) => {
      qc.invalidateQueries({ queryKey: ["ai-research", "backtests", "status", backtestId] });
      qc.invalidateQueries({ queryKey: ["ai-research", "backtests", "history"] });
    },
  });
};

export const useCancelBacktest = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (backtestId: string) => api.cancelBacktest(backtestId),
    onSuccess: (_data, backtestId) => {
      qc.invalidateQueries({ queryKey: ["ai-research", "backtests", "status", backtestId] });
    },
  });
};
