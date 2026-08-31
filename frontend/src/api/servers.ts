// Server-management API calls. Everything routes through the shared
// apiRequest client (bearer + refresh + CSRF + ApiError) — no bare fetch.
// The backend is the only thing that talks to AWS/Lambda/EC2.

import { apiRequest } from "./client";
import type {
  ServerCreate,
  ServerListEntry,
  ServerPatch,
  ServerPowerAction,
  ServerPowerResponse,
} from "./types";

export const createServer = (body: ServerCreate) =>
  apiRequest<ServerListEntry>("/api/servers", { method: "POST", body });

export const updateServer = (serverId: string, body: ServerPatch) =>
  apiRequest<ServerListEntry>(`/api/servers/${encodeURIComponent(serverId)}`, { method: "PATCH", body });

export const deleteServer = (serverId: string) =>
  apiRequest<void>(`/api/servers/${encodeURIComponent(serverId)}`, { method: "DELETE" });

export const powerServer = (serverId: string, action: ServerPowerAction, force = false) =>
  apiRequest<ServerPowerResponse>(`/api/servers/${encodeURIComponent(serverId)}/${action}`, {
    method: "POST",
    query: force ? { force: true } : undefined,
  });
