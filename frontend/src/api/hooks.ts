import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { POLL_INTERVAL_MS } from "@/lib/config";
import { usePollInterval } from "@/realtime/RealtimeProvider";
import * as api from "./endpoints";
import * as serversApi from "./servers";
import * as algosApi from "./algos";
import type {
  AlgoAction,
  AlgoCreate,
  AlgoPatch,
  ServerCreate,
  ServerPatch,
  ServerPowerAction,
} from "./types";

// While the realtime socket is connected, `poll` is false and these
// queries update from the WS stream (+ a one-shot invalidate on connect).
// When it drops, `poll` becomes POLL_INTERVAL_MS — the polling fallback.

export const useHealth = () => {
  const poll = usePollInterval(POLL_INTERVAL_MS);
  return useQuery({ queryKey: ["health"], queryFn: api.getHealth, refetchInterval: poll || POLL_INTERVAL_MS, retry: false });
};

export const useServers = () => {
  const poll = usePollInterval(POLL_INTERVAL_MS);
  return useQuery({ queryKey: ["servers"], queryFn: api.listServers, refetchInterval: poll });
};

export const useServerStatus = (serverId: string | null, live: boolean) =>
  useQuery({
    queryKey: ["server-status", serverId, live],
    queryFn: () => api.getServerStatus(serverId as string, live),
    enabled: !!serverId,
  });

export const useAlgos = () => {
  const poll = usePollInterval(POLL_INTERVAL_MS);
  return useQuery({ queryKey: ["algos"], queryFn: api.listAlgos, refetchInterval: poll });
};

export const useAlgoStatus = (algoId: string | null, serverId: string | null) =>
  useQuery({
    queryKey: ["algo-status", algoId, serverId],
    queryFn: () => api.getAlgoStatus(algoId as string, serverId as string),
    enabled: !!algoId && !!serverId,
  });

export const usePnlToday = (pnlDate?: string) => {
  const poll = usePollInterval(POLL_INTERVAL_MS);
  return useQuery({
    queryKey: ["pnl-today", pnlDate ?? "utc-today"],
    queryFn: () => api.getPnlToday(pnlDate),
    refetchInterval: poll,
  });
};

export const usePnlHistory = (algoId: string | null, serverId: string | null) =>
  useQuery({
    queryKey: ["pnl-history", algoId, serverId],
    queryFn: () => api.getPnlHistory(algoId as string, serverId as string),
    enabled: !!algoId && !!serverId,
    placeholderData: keepPreviousData,
  });

export const usePositions = (algoId: string | null, serverId: string | null) => {
  const poll = usePollInterval(POLL_INTERVAL_MS);
  return useQuery({
    queryKey: ["positions", algoId, serverId],
    queryFn: () => api.getPositions(algoId as string, serverId as string),
    enabled: !!algoId && !!serverId,
    refetchInterval: poll,
    placeholderData: keepPreviousData,
  });
};

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

// --- server management mutations ----------------------------------------
export const useCreateServer = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ServerCreate) => serversApi.createServer(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["servers"] }),
  });
};

export const useUpdateServer = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { serverId: string; body: ServerPatch }) => serversApi.updateServer(v.serverId, v.body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["servers"] }),
  });
};

export const useDeleteServer = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (serverId: string) => serversApi.deleteServer(serverId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["servers"] }),
  });
};

export const useServerPower = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { serverId: string; action: ServerPowerAction; force?: boolean }) =>
      serversApi.powerServer(v.serverId, v.action, v.force),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["servers"] });
      qc.invalidateQueries({ queryKey: ["server-status"] });
    },
  });
};

// --- algorithm management mutations -----------------------------------
export const useCreateAlgo = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AlgoCreate) => algosApi.createAlgo(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["algos"] }),
  });
};

export const useUpdateAlgo = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { algoId: string; serverId: string; body: AlgoPatch }) =>
      algosApi.updateAlgo(v.algoId, v.serverId, v.body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["algos"] }),
  });
};

export const useDeleteAlgo = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { algoId: string; serverId: string; force?: boolean }) =>
      algosApi.deleteAlgo(v.algoId, v.serverId, v.force),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["algos"] }),
  });
};

// --- market data (Stage 19) -------------------------------------------
export const useMarketIndices = () => {
  const poll = usePollInterval(POLL_INTERVAL_MS);
  return useQuery({ queryKey: ["market-indices"], queryFn: api.getMarketIndices, refetchInterval: poll });
};

export const useMarketHealth = () => {
  const poll = usePollInterval(POLL_INTERVAL_MS);
  return useQuery({ queryKey: ["market-health"], queryFn: api.getMarketHealth, refetchInterval: poll || POLL_INTERVAL_MS });
};

export const useMarketSessionStatus = () =>
  useQuery({ queryKey: ["market-session"], queryFn: api.getMarketSessionStatus });

export const useNiftyExpiries = () =>
  useQuery({ queryKey: ["nifty-expiries"], queryFn: api.getNiftyExpiries });

export const useNiftyOptionChain = (expiry: string, range: number) => {
  const poll = usePollInterval(5000);
  return useQuery({
    queryKey: ["nifty-option-chain", expiry, range],
    queryFn: () => api.getNiftyOptionChain(expiry, range),
    refetchInterval: poll || 5000,
    placeholderData: keepPreviousData,
  });
};

export const useMarketCandles = (symbol: string | null, interval = "1minute") =>
  useQuery({
    queryKey: ["market-candles", symbol, interval],
    queryFn: () => api.getMarketCandles(symbol as string, interval),
    enabled: !!symbol,
    placeholderData: keepPreviousData,
  });

export const useUpdateMarketSession = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { session_token: string; api_key?: string; secret_key?: string }) =>
      api.updateMarketSession(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["market-session"] });
      qc.invalidateQueries({ queryKey: ["market-health"] });
    },
  });
};
