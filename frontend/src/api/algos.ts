// Algorithm-management API calls. Process control (start/stop/restart/
// update) stays in endpoints.ts (runAlgoAction) — this module is only the
// registration lifecycle: create / patch / delete.

import { apiRequest } from "./client";
import type { AlgoCreate, AlgoListEntry, AlgoPatch, AlgoRegisterResponse } from "./types";

export const createAlgo = (body: AlgoCreate) =>
  apiRequest<AlgoRegisterResponse>("/api/algos", { method: "POST", body });

export const updateAlgo = (algoId: string, serverId: string, body: AlgoPatch) =>
  apiRequest<AlgoListEntry>(`/api/algos/${encodeURIComponent(algoId)}`, {
    method: "PATCH",
    query: { server_id: serverId },
    body,
  });

export const deleteAlgo = (algoId: string, serverId: string, force = false) =>
  apiRequest<void>(`/api/algos/${encodeURIComponent(algoId)}`, {
    method: "DELETE",
    query: { server_id: serverId, force },
  });
