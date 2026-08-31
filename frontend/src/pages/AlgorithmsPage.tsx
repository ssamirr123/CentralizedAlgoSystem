import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  useAlgos,
  useServers,
  usePnlToday,
  useCreateAlgo,
  useUpdateAlgo,
  useDeleteAlgo,
} from "@/api/hooks";
import type { AlgoAction, AlgoListEntry } from "@/api/types";
import { ApiError } from "@/api/client";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { AlgoFormModal, type AlgoFormValue } from "@/components/AlgoFormModal";
import { useAuth } from "@/auth/AuthContext";
import { useCommandRunner } from "@/lib/useCommandRunner";
import { TRADING_MODE } from "@/lib/config";
import { formatINR, relativeAge, isStale, istDateToday, pnlSign } from "@/lib/format";

type ActState = { algo: AlgoListEntry; action: Exclude<AlgoAction, "update"> };

export function AlgorithmsPage() {
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  const canManage = hasPermission("TRADING_CONTROL");

  const algos = useAlgos();
  const servers = useServers();
  const pnl = usePnlToday(istDateToday());

  const create = useCreateAlgo();
  const update = useUpdateAlgo();
  const del = useDeleteAlgo();
  const runner = useCommandRunner();

  const [q, setQ] = useState("");
  const [serverF, setServerF] = useState("");
  const [statusF, setStatusF] = useState("");

  const [form, setForm] = useState<null | { mode: "create" | "edit"; algo?: AlgoListEntry }>(null);
  const [act, setAct] = useState<ActState | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AlgoListEntry | null>(null);
  const [forceDelete, setForceDelete] = useState(false);

  const serverNames = useMemo(
    () => Array.from(new Set((algos.data ?? []).map((a) => a.server_id))).sort(),
    [algos.data],
  );

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (algos.data ?? []).filter((a) => {
      if (needle && !`${a.algo_id} ${a.server_id}`.toLowerCase().includes(needle)) return false;
      if (serverF && a.server_id !== serverF) return false;
      if (statusF && a.status.toUpperCase() !== statusF) return false;
      return true;
    });
  }, [algos.data, q, serverF, statusF]);

  function submitForm(v: AlgoFormValue) {
    if (form?.mode === "edit" && form.algo) {
      update.mutate(
        {
          algoId: form.algo.algo_id,
          serverId: form.algo.server_id,
          body: { script_path: v.script_path || undefined, enabled: v.enabled },
        },
        { onSuccess: () => setForm(null) },
      );
    } else {
      create.mutate(
        { algo_id: v.algo_id, server_id: v.server_id, script_path: v.script_path || null, enabled: v.enabled },
        { onSuccess: () => setForm(null) },
      );
    }
  }

  const actErr = runner.runs.find((r) => r.status === "FAILED")?.message;

  return (
    <>
      <PageHeader
        title="Algorithms"
        description="Manage trading strategies. Registration, config edits and process control — all via the FastAPI backend."
        actions={
          <button
            className="primary sm"
            disabled={!canManage}
            title={canManage ? undefined : "Requires the TRADING_CONTROL permission"}
            onClick={() => setForm({ mode: "create" })}
          >
            + Add Algorithm
          </button>
        }
      />

      <div className={`inline-note ${TRADING_MODE === "live" ? "warn" : ""}`} style={{ marginBottom: 14 }}>
        <strong>{TRADING_MODE.toUpperCase()} build.</strong> Start / Stop / Restart affect the strategy{" "}
        <em>process</em> only. This UI never places orders. Stop uses the backend SAFE_STOP path.
      </div>

      <div className="filters">
        <div className="field grow">
          <label>Search</label>
          <input placeholder="algorithm or server" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="field">
          <label>Server</label>
          <select value={serverF} onChange={(e) => setServerF(e.target.value)}>
            <option value="">All</option>
            {serverNames.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Status</label>
          <select value={statusF} onChange={(e) => setStatusF(e.target.value)}>
            <option value="">All</option>
            {["RUNNING", "STOPPED", "ERROR", "STARTING", "STOPPING", "UNKNOWN"].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <button className="sm" onClick={() => algos.refetch()}>
          Refresh
        </button>
      </div>

      <QueryBoundary query={algos} empty={(d) => d.length === 0}>
        {() =>
          filtered.length === 0 ? (
            <div className="state">
              No algorithms match the current filter.
              {canManage && (algos.data?.length ?? 0) === 0 && (
                <div style={{ marginTop: 12 }}>
                  <button className="primary" onClick={() => setForm({ mode: "create" })}>
                    + Create an algorithm
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="entity-grid">
              {filtered.map((a) => {
                const key = `${a.algo_id}|${a.server_id}`;
                const today = pnl.data?.[key];
                const running = a.status.toUpperCase() === "RUNNING";
                return (
                  <div className="entity-card" key={key}>
                    <div className="ec-head">
                      <h3>{a.algo_id}</h3>
                      <span className="flex" />
                      <StatusBadge status={a.status} />
                    </div>
                    <dl className="ec-meta">
                      <dt>Server</dt>
                      <dd className="mono">{a.server_id}</dd>
                      <dt>Mode</dt>
                      <dd style={{ textTransform: "uppercase" }}>{TRADING_MODE} · locked</dd>
                      <dt>Enabled</dt>
                      <dd>{a.enabled ? "yes" : "no"}</dd>
                      <dt>Script</dt>
                      <dd className="mono">{a.script_path}</dd>
                    </dl>
                    <div className="ec-stats">
                      <span>
                        P&amp;L today{" "}
                        <span className={`v ${pnlSign(today ?? null)}`}>
                          {today == null ? "—" : formatINR(today)}
                        </span>
                      </span>
                      <span>
                        Heartbeat{" "}
                        <span className={`v ${isStale(a.last_heartbeat) ? "neg" : ""}`}>
                          {relativeAge(a.last_heartbeat)}
                        </span>
                      </span>
                    </div>
                    <div className="ec-actions">
                      <button
                        className="sm"
                        onClick={() =>
                          navigate(
                            `/algorithms/${encodeURIComponent(a.algo_id)}?server=${encodeURIComponent(a.server_id)}`,
                          )
                        }
                      >
                        View
                      </button>
                      <button
                        className="sm"
                        disabled={!canManage}
                        title={canManage ? undefined : "Requires TRADING_CONTROL"}
                        onClick={() => setForm({ mode: "edit", algo: a })}
                      >
                        Edit
                      </button>
                      <button
                        className="sm primary"
                        disabled={!hasPermission("START") || runner.isPending || running}
                        title={hasPermission("START") ? undefined : "Requires START"}
                        onClick={() => runner.run("start", a.algo_id, a.server_id)}
                      >
                        Start
                      </button>
                      <button
                        className="sm"
                        disabled={!hasPermission("RESTART") || runner.isPending}
                        title={hasPermission("RESTART") ? undefined : "Requires RESTART"}
                        onClick={() => setAct({ algo: a, action: "restart" })}
                      >
                        Restart
                      </button>
                      <button
                        className="sm danger"
                        disabled={!hasPermission("STOP") || runner.isPending || !running}
                        title={hasPermission("STOP") ? undefined : "Requires STOP"}
                        onClick={() => setAct({ algo: a, action: "stop" })}
                      >
                        Stop
                      </button>
                      <button
                        className="sm danger"
                        disabled={!canManage}
                        title={canManage ? undefined : "Requires TRADING_CONTROL"}
                        onClick={() => {
                          setForceDelete(false);
                          setDeleteTarget(a);
                        }}
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

      {runner.runs.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <h2>This session's process commands</h2>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Action</th>
                  <th>Algorithm</th>
                  <th>Server</th>
                  <th>Command</th>
                  <th>Status</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {runner.runs.map((r) => (
                  <tr key={r.key}>
                    <td>{r.action}</td>
                    <td>{r.algoId}</td>
                    <td className="mono">{r.serverId}</td>
                    <td className="mono">{r.commandId ?? "—"}</td>
                    <td>
                      <StatusBadge status={r.status} />
                    </td>
                    <td style={{ whiteSpace: "normal", color: "var(--text-dim)" }}>{r.message ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {form && (
        <AlgoFormModal
          mode={form.mode}
          initial={form.algo}
          servers={servers.data ?? []}
          busy={create.isPending || update.isPending}
          error={form.mode === "edit" ? update.error : create.error}
          onSubmit={submitForm}
          onClose={() => setForm(null)}
        />
      )}

      {act && (
        <ConfirmDialog
          open
          title={`${act.action[0].toUpperCase()}${act.action.slice(1)} “${act.algo.algo_id}”?`}
          danger
          busy={runner.isPending}
          confirmLabel={act.action[0].toUpperCase() + act.action.slice(1)}
          body={
            <>
              <p style={{ marginTop: 0 }}>
                Target: <code>{act.algo.algo_id}</code> on <code>{act.algo.server_id}</code> · status{" "}
                <strong>{act.algo.status}</strong>.
              </p>
              <p>
                {act.action === "stop"
                  ? "Uses the backend SAFE_STOP path (reconcile positions, then stop the process). No orders are placed by this UI."
                  : "The backend safety mechanism is used. No orders are placed by this UI."}
              </p>
              {actErr && <p className="form-error">{actErr}</p>}
            </>
          }
          onConfirm={() => {
            runner.run(act.action, act.algo.algo_id, act.algo.server_id);
            setAct(null);
          }}
          onCancel={() => setAct(null)}
        />
      )}

      {deleteTarget && (
        <ConfirmDialog
          open
          title={`Delete algorithm ${deleteTarget.algo_id}?`}
          danger
          busy={del.isPending}
          confirmLabel={forceDelete ? "Force delete" : "Delete Algorithm"}
          body={
            deleteTarget.status.toUpperCase() === "RUNNING" ? (
              <p style={{ marginTop: 0 }} className="form-error">
                This algorithm is currently RUNNING. Stop it before deleting.
              </p>
            ) : (
              <>
                <p style={{ marginTop: 0 }}>
                  Removes the registration for <code>{deleteTarget.algo_id}</code> on{" "}
                  <code>{deleteTarget.server_id}</code>. Trades, P&amp;L, logs, heartbeats and runs are{" "}
                  <strong>not</strong> deleted unless you force it.
                </p>
                {del.error instanceof ApiError && (
                  <p className="form-error">
                    {del.error.status}: {del.error.detail}
                    {del.error.status === 409 && !forceDelete && (
                      <>
                        {" "}
                        <button className="sm danger" style={{ marginLeft: 8 }} onClick={() => setForceDelete(true)}>
                          Enable force
                        </button>
                      </>
                    )}
                  </p>
                )}
                {forceDelete && (
                  <p className="form-error">Force also purges all historical trades / P&amp;L / logs for this algo.</p>
                )}
              </>
            )
          }
          onConfirm={() => {
            if (deleteTarget.status.toUpperCase() === "RUNNING") return;
            del.mutate(
              { algoId: deleteTarget.algo_id, serverId: deleteTarget.server_id, force: forceDelete },
              { onSuccess: () => setDeleteTarget(null) },
            );
          }}
          onCancel={() => {
            setDeleteTarget(null);
            del.reset();
          }}
        />
      )}
    </>
  );
}
