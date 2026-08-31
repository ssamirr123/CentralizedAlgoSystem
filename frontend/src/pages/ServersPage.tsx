import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  useServers,
  useAlgos,
  useCreateServer,
  useUpdateServer,
  useDeleteServer,
  useServerPower,
} from "@/api/hooks";
import type { ServerListEntry, ServerPowerAction } from "@/api/types";
import { ApiError } from "@/api/client";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ServerFormModal, type ServerFormValue } from "@/components/ServerFormModal";
import { useAuth } from "@/auth/AuthContext";
import { relativeAge } from "@/lib/format";

type PowerState = { server: ServerListEntry; action: ServerPowerAction; force: boolean };

export function ServersPage() {
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  const canManage = hasPermission("TRADING_CONTROL");

  const servers = useServers();
  const algos = useAlgos();

  const create = useCreateServer();
  const update = useUpdateServer();
  const del = useDeleteServer();
  const power = useServerPower();

  const [q, setQ] = useState("");
  const [region, setRegion] = useState("");
  const [statusF, setStatusF] = useState("");

  const [form, setForm] = useState<null | { mode: "create" | "edit"; server?: ServerListEntry }>(null);
  const [powerState, setPowerState] = useState<PowerState | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ServerListEntry | null>(null);

  const algoCountByServer = useMemo(() => {
    const m: Record<string, number> = {};
    for (const a of algos.data ?? []) m[a.server_id] = (m[a.server_id] ?? 0) + 1;
    return m;
  }, [algos.data]);

  const regions = useMemo(
    () => Array.from(new Set((servers.data ?? []).map((s) => s.region))).sort(),
    [servers.data],
  );

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (servers.data ?? []).filter((s) => {
      if (needle && !`${s.server_id} ${s.ec2_instance_id}`.toLowerCase().includes(needle)) return false;
      if (region && s.region !== region) return false;
      if (statusF && s.status.toUpperCase() !== statusF) return false;
      return true;
    });
  }, [servers.data, q, region, statusF]);

  function submitForm(v: ServerFormValue) {
    if (form?.mode === "edit" && form.server) {
      update.mutate(
        {
          serverId: form.server.server_id,
          body: { server_id: v.server_id, ec2_instance_id: v.ec2_instance_id, region: v.region, os: v.os, repo_path: v.repo_path },
        },
        { onSuccess: () => setForm(null) },
      );
    } else {
      create.mutate(
        { server_id: v.server_id, ec2_instance_id: v.ec2_instance_id, region: v.region, os: v.os, repo_path: v.repo_path, auto_provision: v.auto_provision },
        { onSuccess: () => setForm(null) },
      );
    }
  }

  function runPower() {
    if (!powerState) return;
    power.mutate(
      { serverId: powerState.server.server_id, action: powerState.action, force: powerState.force },
      {
        onSuccess: () => setPowerState(null),
        onError: (e) => {
          // Safe-stop guard: 409 means a trading process is still alive.
          // Keep the dialog open and offer an explicit force.
          if (e instanceof ApiError && e.status === 409) {
            setPowerState((p) => (p ? { ...p, force: true } : p));
          }
        },
      },
    );
  }

  const powerErr = power.error instanceof ApiError ? power.error : null;

  return (
    <>
      <PageHeader
        title="Servers"
        description="Manage your trading infrastructure. React → FastAPI → Lambda → SSM → EC2."
        actions={
          <button
            className="primary sm"
            disabled={!canManage}
            title={canManage ? undefined : "Requires the TRADING_CONTROL permission"}
            onClick={() => setForm({ mode: "create" })}
          >
            + Add Server
          </button>
        }
      />

      <div className="filters">
        <div className="field grow">
          <label>Search</label>
          <input placeholder="name or instance id" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="field">
          <label>Region</label>
          <select value={region} onChange={(e) => setRegion(e.target.value)}>
            <option value="">All</option>
            {regions.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Status</label>
          <select value={statusF} onChange={(e) => setStatusF(e.target.value)}>
            <option value="">All</option>
            {["RUNNING", "STOPPED", "STARTING", "STOPPING", "REBOOTING", "UNKNOWN"].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <button className="sm" onClick={() => servers.refetch()}>
          Refresh
        </button>
      </div>

      <QueryBoundary query={servers} empty={(d) => d.length === 0}>
        {() =>
          filtered.length === 0 ? (
            <div className="state">
              No servers match the current filter.
              {canManage && (servers.data?.length ?? 0) === 0 && (
                <div style={{ marginTop: 12 }}>
                  <button className="primary" onClick={() => setForm({ mode: "create" })}>
                    + Add your first server
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="entity-grid">
              {filtered.map((s) => {
                const activeAlgos = algoCountByServer[s.server_id] ?? 0;
                return (
                  <div className="entity-card" key={s.server_id}>
                    <div className="ec-head">
                      <h3>{s.server_id}</h3>
                      <span className="flex" />
                      <StatusBadge status={s.status} />
                    </div>
                    <dl className="ec-meta">
                      <dt>EC2</dt>
                      <dd className="mono">{s.ec2_instance_id}</dd>
                      <dt>Region</dt>
                      <dd>{s.region}</dd>
                      <dt>OS</dt>
                      <dd>{s.os}</dd>
                      <dt>Provisioning</dt>
                      <dd>
                        {s.provisioning_status}
                        {s.provisioning_message ? ` — ${s.provisioning_message}` : ""}
                      </dd>
                    </dl>
                    <div className="ec-stats">
                      <span>
                        Heartbeat <span className="v">{relativeAge(s.last_heartbeat)}</span>
                      </span>
                      <span>
                        Active algos <span className="v">{activeAlgos}</span>
                      </span>
                    </div>
                    <div className="ec-actions">
                      <button className="sm" onClick={() => navigate(`/servers/${encodeURIComponent(s.server_id)}`)}>
                        View
                      </button>
                      <button
                        className="sm"
                        disabled={!canManage}
                        title={canManage ? undefined : "Requires TRADING_CONTROL"}
                        onClick={() => setForm({ mode: "edit", server: s })}
                      >
                        Edit
                      </button>
                      <button
                        className="sm primary"
                        disabled={!canManage || power.isPending}
                        title={canManage ? undefined : "Requires TRADING_CONTROL"}
                        onClick={() => setPowerState({ server: s, action: "start", force: false })}
                      >
                        Start
                      </button>
                      <button
                        className="sm"
                        disabled={!canManage || power.isPending}
                        title={canManage ? undefined : "Requires TRADING_CONTROL"}
                        onClick={() => setPowerState({ server: s, action: "restart", force: false })}
                      >
                        Restart
                      </button>
                      <button
                        className="sm danger"
                        disabled={!canManage || power.isPending}
                        title={canManage ? undefined : "Requires TRADING_CONTROL"}
                        onClick={() => setPowerState({ server: s, action: "stop", force: false })}
                      >
                        Stop
                      </button>
                      <button
                        className="sm danger"
                        disabled={!canManage}
                        title={canManage ? undefined : "Requires TRADING_CONTROL"}
                        onClick={() => setDeleteTarget(s)}
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )
        }
      </QueryBoundary>

      {form && (
        <ServerFormModal
          mode={form.mode}
          initial={form.server}
          busy={create.isPending || update.isPending}
          error={form.mode === "edit" ? update.error : create.error}
          onSubmit={submitForm}
          onClose={() => setForm(null)}
        />
      )}

      {powerState && (
        <ConfirmDialog
          open
          title={`${powerState.action[0].toUpperCase()}${powerState.action.slice(1)} ${powerState.server.server_id}?`}
          danger={powerState.action !== "start"}
          busy={power.isPending}
          confirmLabel={
            powerState.force
              ? "Force " + powerState.action
              : powerState.action[0].toUpperCase() + powerState.action.slice(1)
          }
          body={
            <>
              <p style={{ marginTop: 0 }}>
                EC2: <strong>{powerState.server.status}</strong> · Active algorithms:{" "}
                <strong>{algoCountByServer[powerState.server.server_id] ?? 0}</strong>
              </p>
              {powerState.action !== "start" && (
                <p>
                  Stopping / rebooting this server may interrupt trading processes. The backend safe-stop guard
                  will block it if a strategy is still alive.
                </p>
              )}
              {powerErr && (
                <p className="form-error">
                  {powerErr.status}: {powerErr.detail}
                </p>
              )}
              {powerState.force && (
                <p className="form-error">
                  Force bypasses the safe-stop guard. Only do this if you know the strategy has already shut
                  down cleanly.
                </p>
              )}
            </>
          }
          onConfirm={runPower}
          onCancel={() => {
            setPowerState(null);
            power.reset();
          }}
        />
      )}

      {deleteTarget && (
        <ConfirmDialog
          open
          title={`Delete server ${deleteTarget.server_id}?`}
          danger
          busy={del.isPending}
          confirmLabel="Delete Server"
          body={
            (algoCountByServer[deleteTarget.server_id] ?? 0) > 0 ? (
              <p style={{ marginTop: 0 }} className="form-error">
                This server has {algoCountByServer[deleteTarget.server_id]} registered algorithm(s). Remove them
                first — the backend will reject the delete.
              </p>
            ) : (
              <>
                <p style={{ marginTop: 0 }}>
                  Removes <code>{deleteTarget.server_id}</code> from the control plane. It does <strong>not</strong>{" "}
                  terminate the AWS EC2 instance, and historical trading data is preserved.
                </p>
                {del.error instanceof ApiError && (
                  <p className="form-error">
                    {del.error.status}: {del.error.detail}
                  </p>
                )}
              </>
            )
          }
          onConfirm={() =>
            del.mutate(deleteTarget.server_id, {
              onSuccess: () => setDeleteTarget(null),
            })
          }
          onCancel={() => {
            setDeleteTarget(null);
            del.reset();
          }}
        />
      )}
    </>
  );
}
