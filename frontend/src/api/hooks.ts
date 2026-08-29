import { useQuery, useMutation, keepPreviousData } from "@tanstack/react-query";
import { POLL_INTERVAL_MS } from "@/lib/config";
import * as api from "./endpoints";
import type { AlgoAction } from "./types";

export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: api.getHealth, refetchInterval: POLL_INTERVAL_MS, retry: false });

export const useServers = () =>
  useQuery({ queryKey: ["servers"], queryFn: api.listServers, refetchInterval: POLL_INTERVAL_MS });

export const useServerStatus = (serverId: string | null, live: boolean) =>
  useQuery({
    queryKey: ["server-status", serverId, live],
    queryFn: () => api.getServerStatus(serverId as string, live),
    enabled: !!serverId,
  });

export const useAlgos = () =>
  useQuery({ queryKey: ["algos"], queryFn: api.listAlgos, refetchInterval: POLL_INTERVAL_MS });

export const useAlgoStatus = (algoId: string | null, serverId: string | null) =>
  useQuery({
    queryKey: ["algo-status", algoId, serverId],
    queryFn: () => api.getAlgoStatus(algoId as string, serverId as string),
    enabled: !!algoId && !!serverId,
  });

export const useStrategyHeartbeats = () =>
  useQuery({
    queryKey: ["strategy-heartbeats"],
    queryFn: api.listStrategyHeartbeats,
    refetchInterval: POLL_INTERVAL_MS,
  });

export const usePnlToday = (pnlDate?: string) =>
  useQuery({
    queryKey: ["pnl-today", pnlDate ?? "utc-today"],
    queryFn: () => api.getPnlToday(pnlDate),
    refetchInterval: POLL_INTERVAL_MS,
  });

export const usePnlHistory = (algoId: string | null, serverId: string | null) =>
  useQuery({
    queryKey: ["pnl-history", algoId, serverId],
    queryFn: () => api.getPnlHistory(algoId as string, serverId as string),
    enabled: !!algoId && !!serverId,
    placeholderData: keepPreviousData,
  });

export const usePositions = (algoId: string | null, serverId: string | null) =>
  useQuery({
    queryKey: ["positions", algoId, serverId],
    queryFn: () => api.getPositions(algoId as string, serverId as string),
    enabled: !!algoId && !!serverId,
    refetchInterval: POLL_INTERVAL_MS,
    placeholderData: keepPreviousData,
  });

export const useTrades = (algoId: string | null, serverId: string | null, limit = 100) =>
  useQuery({
    queryKey: ["trades", algoId, serverId, limit],
    queryFn: () => api.getTrades(algoId as string, serverId as string, limit),
    enabled: !!algoId && !!serverId,
    placeholderData: keepPreviousData,
  });

export const useLogs = (q: api.LogQuery | null) =>
  useQuery({
    queryKey: ["logs", q],
    queryFn: () => api.getLogs(q as api.LogQuery),
    enabled: !!q && !!q.algo_id && !!q.server_id,
    placeholderData: keepPreviousData,
  });

export const useAlgoActionMutation = () =>
  useMutation({
    mutationFn: (v: { action: AlgoAction; algoId: string; serverId: string; requestedBy?: string }) =>
      api.runAlgoAction(v.action, v.algoId, v.serverId, v.requestedBy),
  });
