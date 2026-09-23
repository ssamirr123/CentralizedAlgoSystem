// AI Options Research API client -- trading/api/ai_options_research_routes.py.
import { apiRequest } from "./client";
import type {
  HistoryResponse, OptionsResearchRequest, ResearchResult, ResearchStatus, SnapshotPreview, StagesOut,
} from "./aiOptionsResearchTypes";

export const getSnapshotPreview = (underlying: string, expiry: string, strikeWindow?: number) =>
  apiRequest<SnapshotPreview>("/api/ai-options-research/preview", {
    query: { underlying, expiry, strike_window: strikeWindow },
  });

export const submitOptionsResearch = (body: OptionsResearchRequest) =>
  apiRequest<ResearchStatus>("/api/ai-options-research", { method: "POST", body });

export const getOptionsResearchResult = (researchId: string) =>
  apiRequest<ResearchResult>(`/api/ai-options-research/${researchId}`);

export const getOptionsResearchStatus = (researchId: string) =>
  apiRequest<ResearchStatus>(`/api/ai-options-research/${researchId}/status`);

export const getOptionsResearchStages = (researchId: string) =>
  apiRequest<StagesOut>(`/api/ai-options-research/${researchId}/stages`);

export const getOptionsResearchHistory = (page = 1, pageSize = 25) =>
  apiRequest<HistoryResponse>("/api/ai-options-research/history", { query: { page, page_size: pageSize } });
