import { useCallback, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAlgoActionMutation } from "@/api/hooks";
import { getCommand } from "@/api/endpoints";
import type { AlgoAction, CommandResponse } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { AlgoPicker, type AlgoRef } from "@/components/AlgoPicker";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { StatusBadge } from "@/components/StatusBadge";
import { IS_LIVE, LIVE_EXECUTION_ENABLED, TRADING_MODE, type Permission } from "@/lib/config";
import { formatIST } from "@/lib/format";
import { useAuth } from "@/auth/AuthContext";

const ACTIONS: { action: AlgoAction; label: string; danger?: boolean; permission: Permission }[] = [
  { action: "start", label: "Start", permission: "START" },
  { action: "stop", label: "Stop", danger: true, permission: "STOP" },
  { action: "restart", label: "Restart", danger: true, permission: "RESTART" },
  { action: "update", label: "Update code", permission: "TRADING_CONTROL" },
];

interface LogRow {
  at: string;
  action: AlgoAction;
  algoId: string;
  serverId: string;
  commandId: number | null;
  jobId: string | null;
  status: string;
  message: string | null;
}

const TERMINAL = new Set(["RUNNING", "STOPPED", "ERROR", "FAILED", "SUCCESS", "UNKNOWN", "UPDATED"]);

export function CommandsPage() {
  const { hasPermission, hasAny } = useAuth();
  const canControlAnything = hasAny(["START", "STOP", "RESTART", "TRADING_CONTROL"]);
  const [ref, setRef] = useState<AlgoRef | null>(null);
  const [pending, setPending] = useState<AlgoAction | null>(null);
  const [rows, setRows] = useState<LogRow[]>([]);
  const mutation = useAlgoActionMutation();
  const qc = useQueryClient();
  const pollers = useRef<Set<number>>(new Set());

  const patchRow = useCallback((commandId: number, patch: Partial<LogRow>) => {
    setRows((prev) => prev.map((r) => (r.commandId === commandId ? { ...r, ...patch } : r)));
  }, []);

  const pollCommand = useCallback(
    (commandId: number) => {
      let tries = 0;
      const tick = async () => {
        tries += 1;
        try {
          const res: CommandResponse = await getCommand(commandId);
          patchRow(commandId, { status: res.status, message: res.message ?? null });
          if (!TERMINAL.has(res.status.toUpperCase()) && tries < 40) {
            window.setTimeout(tick, 3000);
          } else {
            pollers.current.delete(commandId);
            qc.invalidateQueries({ queryKey: ["algos"] });
            qc.invalidateQueries({ queryKey: ["algo-status"] });
          }
        } catch (e) {
          patchRow(commandId, { message: e instanceof Error ? e.message : "poll failed" });
          pollers.current.delete(commandId);
        }
      };
      pollers.current.add(commandId);
      window.setTimeout(tick, 2000);
    },
    [patchRow, qc],
  );

  async function confirm() {
    if (!ref || !pending) return;
    const action = pending;
    try {
      const res = await mutation.mutateAsync({
        action,
        algoId: ref.algoId,
        serverId: ref.serverId,
        requestedBy: "control-center-ui",
      });
      const row: LogRow = {
        at: new Date().toISOString(),
        action,
        algoId: ref.algoId,
        serverId: ref.serverId,
        commandId: res.command_id,
        jobId: res.job_id,
        status: res.status,
        message: res.message ?? null,
      };
      setRows((prev) => [row, ...prev].slice(0, 50));
      if (res.command_id != null) pollCommand(res.command_id);
    } catch (e) {
      setRows((prev) =>
        [
          {
            at: new Date().toISOString(),
            action,
            algoId: ref.algoId,
            serverId: ref.serverId,
            commandId: null,
            jobId: null,
            status: "FAILED",
            message: e instanceof Error ? e.message : "request failed",
          },
          ...prev,
        ].slice(0, 50),
      );
    } finally {
      setPending(null);
    }
  }

  return (
    <>
      <PageHeader
        title="Commands"
        description="Process control for a registered strategy (POST /api/algo/{start,stop,restart,update})."
      />

      <div className={`inline-note ${IS_LIVE ? "warn" : ""}`} style={{ marginBottom: 14 }}>
        {IS_LIVE ? (
          <>
            <strong>LIVE build.</strong> These are process-control commands only. Live order execution is{" "}
            <strong>not implemented</strong> in this UI ({String(LIVE_EXECUTION_ENABLED)}). The strategy itself decides
            what orders to place based on the server's <code>TRADING_MODE</code>.
          </>
        ) : (
          <>
            <strong>PAPER build.</strong> Start/stop/restart/update only affect the strategy <em>process</em>. No orders
            are placed by this UI.
          </>
        )}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="toolbar">
          <AlgoPicker value={ref} onChange={setRef} />
        </div>
        <div className="row-actions" style={{ flexWrap: "wrap" }}>
          {ACTIONS.map((a) => {
            const allowed = hasPermission(a.permission);
            return (
              <button
                key={a.action}
                className={a.danger ? "danger" : "primary"}
                disabled={!ref || mutation.isPending || !allowed}
                title={allowed ? undefined : `Requires the ${a.permission} permission`}
                onClick={() => setPending(a.action)}
              >
                {a.label}
              </button>
            );
          })}
        </div>
        {!canControlAnything && (
          <div className="inline-note warn" style={{ marginTop: 10 }}>
            Your role has no process-control permissions — these actions are read-only for you.
          </div>
        )}
      </div>

      <div className="card">
        <h2>This session's commands</h2>
        {rows.length === 0 ? (
          <div className="state">No commands issued yet.</div>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Time (IST)</th>
                  <th>Action</th>
                  <th>Strategy</th>
                  <th>Server</th>
                  <th>Command ID</th>
                  <th>Job ID</th>
                  <th>Status</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.commandId ?? "x"}-${i}`}>
                    <td>{formatIST(r.at)}</td>
                    <td>{r.action}</td>
                    <td>{r.algoId}</td>
                    <td className="mono">{r.serverId}</td>
                    <td className="mono">{r.commandId ?? "—"}</td>
                    <td className="mono">{r.jobId ?? "—"}</td>
                    <td>
                      <StatusBadge status={r.status} />
                    </td>
                    <td style={{ whiteSpace: "normal", color: "var(--text-dim)" }}>{r.message ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={pending !== null}
        title={`${pending ? pending[0].toUpperCase() + pending.slice(1) : ""} “${ref?.algoId ?? ""}”?`}
        danger={pending === "stop" || pending === "restart"}
        busy={mutation.isPending}
        confirmLabel={pending ? pending[0].toUpperCase() + pending.slice(1) : "Confirm"}
        body={
          <>
            <p style={{ marginTop: 0 }}>
              Target: <code>{ref?.algoId}</code> on <code>{ref?.serverId}</code>.
            </p>
            <p>
              This sends a <strong>{TRADING_MODE.toUpperCase()}</strong> process-control command. It does not place or
              cancel any orders.
            </p>
          </>
        }
        onConfirm={confirm}
        onCancel={() => setPending(null)}
      />
    </>
  );
}
