// AI Research Engine API client -- follows the same apiRequest<T>() pattern
// as api/endpoints.ts. Every function here maps 1:1 to a real backend route
// (trading/ai_research/router.py); nothing is invented.
import { apiRequest } from "./client";
import type {
  MemoryEntry,
  ResearchConfigOptions,
  ResearchDisabled,
  ResearchHistoryFilters,
  ResearchHistoryPage,
  ResearchRequest,
  ResearchResult,
  ResearchStatus,
  ResearchSubmitted,
  ResumeRequested,
  Stages,
} from "./aiResearchTypes";

export const createResearch = (body: ResearchRequest) =>
  apiRequest<ResearchSubmitted | ResearchDisabled>("/api/ai-research", { method: "POST", body });

export const getResearch = (researchId: string) =>
  apiRequest<ResearchResult | ResearchDisabled>(`/api/ai-research/${encodeURIComponent(researchId)}`);

export const getResearchStatus = (researchId: string) =>
  apiRequest<ResearchStatus | ResearchDisabled>(`/api/ai-research/${encodeURIComponent(researchId)}/status`);

export const getResearchStages = (researchId: string) =>
  apiRequest<Stages | ResearchDisabled>(`/api/ai-research/${encodeURIComponent(researchId)}/stages`);

export const getResearchHistory = (filters: ResearchHistoryFilters = {}) =>
  apiRequest<ResearchHistoryPage | ResearchDisabled>("/api/ai-research/history", {
    query: {
      page: filters.page ?? 1,
      page_size: filters.page_size ?? 50,
      symbol: filters.symbol,
      status: filters.status,
      date_from: filters.date_from,
      date_to: filters.date_to,
      provider: filters.provider,
    },
  });

export const getConfigOptions = () => apiRequest<ResearchConfigOptions>("/api/ai-research/config/options");

export const getDecisionMemory = (limit = 50) =>
  apiRequest<MemoryEntry[] | ResearchDisabled>("/api/ai-research/memory", { query: { limit } });

export const getDecisionMemoryEntry = (memoryId: string) =>
  apiRequest<MemoryEntry | ResearchDisabled>(`/api/ai-research/memory/${encodeURIComponent(memoryId)}`);

export const resumeResearch = (researchId: string) =>
  apiRequest<ResumeRequested | ResearchDisabled>(`/api/ai-research/${encodeURIComponent(researchId)}/resume`, {
    method: "POST",
  });
