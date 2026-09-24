// AI Research Backtesting API client -- follows the same apiRequest<T>()
// pattern as aiResearch.ts; every function maps 1:1 to a real backend
// route (trading/ai_research/backtest/router.py).
import { apiRequest } from "./client";
import type { ResearchDisabled } from "./aiResearchTypes";
import type {
  BacktestCancelled,
  BacktestDetail,
  BacktestEstimate,
  BacktestHistoryPage,
  BacktestMetrics,
  BacktestRequest,
  BacktestResumed,
  BacktestRunsPage,
  BacktestStatus,
  BacktestSubmitted,
  DataQualityMatrix,
} from "./aiResearchBacktestTypes";

export const submitBacktest = (body: BacktestRequest) =>
  apiRequest<BacktestSubmitted | ResearchDisabled>("/api/ai-research/backtests", { method: "POST", body });

export const estimateBacktest = (body: BacktestRequest) =>
  apiRequest<BacktestEstimate | ResearchDisabled>("/api/ai-research/backtests/estimate", { method: "POST", body });

export const getBacktestHistory = (params: { page?: number; page_size?: number; status?: string } = {}) =>
  apiRequest<BacktestHistoryPage | ResearchDisabled>("/api/ai-research/backtests", {
    query: { page: params.page ?? 1, page_size: params.page_size ?? 20, status: params.status },
  });

export const getBacktest = (backtestId: string) =>
  apiRequest<BacktestDetail | ResearchDisabled>(`/api/ai-research/backtests/${encodeURIComponent(backtestId)}`);

export const getBacktestStatus = (backtestId: string) =>
  apiRequest<BacktestStatus | ResearchDisabled>(`/api/ai-research/backtests/${encodeURIComponent(backtestId)}/status`);

export const getBacktestRuns = (backtestId: string, params: { page?: number; page_size?: number } = {}) =>
  apiRequest<BacktestRunsPage | ResearchDisabled>(`/api/ai-research/backtests/${encodeURIComponent(backtestId)}/runs`, {
    query: { page: params.page ?? 1, page_size: params.page_size ?? 50 },
  });

export const getBacktestMetrics = (backtestId: string) =>
  apiRequest<BacktestMetrics | ResearchDisabled>(`/api/ai-research/backtests/${encodeURIComponent(backtestId)}/metrics`);

export const getDataQuality = () => apiRequest<DataQualityMatrix>("/api/ai-research/backtests/data-quality");

export const resumeBacktest = (backtestId: string) =>
  apiRequest<BacktestResumed | ResearchDisabled>(`/api/ai-research/backtests/${encodeURIComponent(backtestId)}/resume`, {
    method: "POST",
  });

export const cancelBacktest = (backtestId: string) =>
  apiRequest<BacktestCancelled | ResearchDisabled>(`/api/ai-research/backtests/${encodeURIComponent(backtestId)}/cancel`, {
    method: "POST",
  });
