import { useState } from "react";
import { useExecutionAuditLog } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { formatIST } from "@/lib/format";

export function TccLogsPage() {
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const logs = useExecutionAuditLog({ action: action || undefined, outcome: outcome || undefined, limit: 200 });

  return (
    <>
      <PageHeader
        title="Logs"
        description="Audit trail for every Trading Control Center action — strategy start/stop, assignment changes, kill-switch engage/disengage, and permission denials. Backed by the same audit_log table every control-center action writes to."
      />

      <div className="filters">
        <div className="field">
          <label>Action</label>
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">All</option>
            <option value="STRATEGY_STARTED">STRATEGY_STARTED</option>
            <option value="STRATEGY_STOPPED">STRATEGY_STOPPED</option>
            <option value="ASSIGNMENT_SET">ASSIGNMENT_SET</option>
            <option value="KILL_SWITCH_ENGAGED">KILL_SWITCH_ENGAGED</option>
            <option value="KILL_SWITCH_DISENGAGED">KILL_SWITCH_DISENGAGED</option>
            <option value="PERMISSION_DENIED">PERMISSION_DENIED</option>
          </select>
        </div>
        <div className="field">
          <label>Outcome</label>
          <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
            <option value="">All</option>
            <option value="success">success</option>
            <option value="denied">denied</option>
            <option value="error">error</option>
          </select>
        </div>
        <button className="sm" onClick={() => logs.refetch()}>
          Refresh
        </button>
      </div>

      <QueryBoundary query={logs} empty={(d) => d.length === 0}>
        {(list) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {list.map((entry) => (
                  <tr key={entry.id}>
                    <td className="mono">{formatIST(entry.timestamp)}</td>
                    <td>{entry.actor_label || entry.actor}</td>
                    <td>{entry.action}</td>
                    <td className="mono">{entry.target ?? "—"}</td>
                    <td>
                      <span className={`badge ${entry.outcome === "success" ? "running" : "error"}`}>
                        <span className="dot" />
                        {entry.outcome.toUpperCase()}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryBoundary>
    </>
  );
}
