// Polling lifecycle (Section 30): react-query's refetchInterval naturally
// avoids overlapping requests (it waits for the in-flight fetch before
// scheduling the next), stops when the component/query unmounts, and we
// return `false` from refetchInterval once the job is COMPLETED/FAILED so
// it never polls a finished job forever. Cadence: 2s, per Phase 3's own
// recommendation (router.py's module docstring) -- a single research run
// is a one-off action, not a high-frequency shared feed.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as api from "./aiResearch";
import { isDisabled } from "./aiResearchTypes";
import type { ResearchHistoryFilters, ResearchRequest } from "./aiResearchTypes";

const POLL_MS = 2000;

export const useAiResearchConfig = () =>
  useQuery({ queryKey: ["ai-research", "config"], queryFn: api.getConfigOptions });

export const useSubmitResearch = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ResearchRequest) => api.createResearch(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-research", "history"] });
    },
  });
};

export const useResearchStatus = (researchId: string | null) =>
  useQuery({
    queryKey: ["ai-research", "status", researchId],
    queryFn: () => api.getResearchStatus(researchId as string),
    enabled: !!researchId,
    refetchInterval: (query) => {
      const d = query.state.data;
      if (!d || isDisabled(d)) return false;
      return d.status === "COMPLETED" || d.status === "FAILED" ? false : POLL_MS;
    },
  });

export const useResearchStages = (researchId: string | null, activeStatus: string | undefined) =>
  useQuery({
    queryKey: ["ai-research", "stages", researchId],
    queryFn: () => api.getResearchStages(researchId as string),
    enabled: !!researchId,
    refetchInterval: activeStatus === "COMPLETED" || activeStatus === "FAILED" ? false : POLL_MS,
  });

export const useResearchResult = (researchId: string | null, status: string | undefined) =>
  useQuery({
    queryKey: ["ai-research", "result", researchId],
    queryFn: () => api.getResearch(researchId as string),
    enabled: !!researchId && (status === "COMPLETED" || status === "FAILED"),
  });

export const useResearchHistory = (filters: ResearchHistoryFilters = {}) =>
  useQuery({
    queryKey: ["ai-research", "history", filters],
    queryFn: () => api.getResearchHistory(filters),
  });

export const useDecisionMemory = (limit = 50) =>
  useQuery({ queryKey: ["ai-research", "memory", limit], queryFn: () => api.getDecisionMemory(limit) });

export const useResumeResearch = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (researchId: string) => api.resumeResearch(researchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-research", "history"] });
    },
  });
};
