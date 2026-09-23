import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as api from "./aiOptionsResearch";
import type { OptionsResearchRequest } from "./aiOptionsResearchTypes";

export const useSnapshotPreview = (underlying: string, expiry: string, strikeWindow?: number) =>
  useQuery({
    queryKey: ["ai-options-research", "preview", underlying, expiry, strikeWindow],
    queryFn: () => api.getSnapshotPreview(underlying, expiry, strikeWindow),
    enabled: !!underlying,
  });

export const useSubmitOptionsResearch = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: OptionsResearchRequest) => api.submitOptionsResearch(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ai-options-research", "history"] }),
  });
};

const isActive = (status?: string) => status === "QUEUED" || status === "RUNNING";

export const useOptionsResearchStatus = (researchId: string) =>
  useQuery({
    queryKey: ["ai-options-research", "status", researchId],
    queryFn: () => api.getOptionsResearchStatus(researchId),
    enabled: !!researchId,
    refetchInterval: (query) => (isActive(query.state.data?.status) ? 2000 : false),
  });

export const useOptionsResearchStages = (researchId: string, jobStatus?: string) =>
  useQuery({
    queryKey: ["ai-options-research", "stages", researchId],
    queryFn: () => api.getOptionsResearchStages(researchId),
    enabled: !!researchId,
    refetchInterval: isActive(jobStatus) ? 2000 : false,
  });

export const useOptionsResearchResult = (researchId: string, jobStatus?: string) =>
  useQuery({
    queryKey: ["ai-options-research", "result", researchId],
    queryFn: () => api.getOptionsResearchResult(researchId),
    enabled: !!researchId && (jobStatus === "COMPLETED" || jobStatus === "FAILED"),
  });

export const useOptionsResearchHistory = (page = 1, pageSize = 25) =>
  useQuery({
    queryKey: ["ai-options-research", "history", page, pageSize],
    queryFn: () => api.getOptionsResearchHistory(page, pageSize),
  });
