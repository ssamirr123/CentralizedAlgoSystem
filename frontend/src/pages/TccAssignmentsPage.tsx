import { useState } from "react";
import {
  useCreateExecutionAssignment,
  useExecutionAccounts,
  useExecutionAssignments,
  useExecutionModes,
  useExecutionStrategies,
} from "@/api/hooks";
import type { ExecutionModeValue } from "@/api/types";
import { ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { ExecutionModeSelect } from "@/components/ExecutionModeSelect";
import { brokerLabel } from "@/lib/format";

export function TccAssignmentsPage() {
  const { hasPermission } = useAuth();
  const canAssign = hasPermission("TRADING_CONTROL");

  const assignments = useExecutionAssignments();
  const strategies = useExecutionStrategies();
  const accounts = useExecutionAccounts();
  const modes = useExecutionModes();
  const create = useCreateExecutionAssignment();

  const [strategyId, setStrategyId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [executionMode, setExecutionMode] = useState<ExecutionModeValue | "">("");
  const [riskProfile, setRiskProfile] = useState("default");

  function submit() {
    if (!strategyId || !accountId) return;
    create.mutate({
      strategy_id: strategyId,
      account_id: accountId,
      execution_mode: executionMode || null,
      risk_profile: riskProfile,
    });
  }

  return (
    <>
      <PageHeader
        title="Strategy Assignments"
        description="Route a strategy to a trading account (Phase 1/7). A strategy is never wired to a broker directly — only to an account, whose broker is an implementation detail."
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>New assignment</h2>
        <div className="filters">
          <div className="field">
            <label>Strategy</label>
            <select value={strategyId} onChange={(e) => setStrategyId(e.target.value)} disabled={!canAssign}>
              <option value="">Select…</option>
              {(strategies.data ?? []).map((s) => (
                <option key={s.strategy_id} value={s.strategy_id}>
                  {s.strategy_id}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Account</label>
            <select value={accountId} onChange={(e) => setAccountId(e.target.value)} disabled={!canAssign}>
              <option value="">Select…</option>
              {(accounts.data ?? []).map((a) => (
                <option key={a.account_id} value={a.account_id}>
                  {a.account_id} — {brokerLabel(a.broker_id)}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Execution mode</label>
            <ExecutionModeSelect modes={modes.data} value={executionMode} onChange={setExecutionMode} disabled={!canAssign} />
          </div>
          <div className="field">
            <label>Risk profile</label>
            <input value={riskProfile} onChange={(e) => setRiskProfile(e.target.value)} disabled={!canAssign} />
          </div>
          <button
            className="primary sm"
            disabled={!canAssign || !strategyId || !accountId || create.isPending}
            title={canAssign ? undefined : "Requires TRADING_CONTROL"}
            onClick={submit}
          >
            {create.isPending ? "Saving…" : "Create assignment"}
          </button>
        </div>
        {create.error instanceof ApiError && <p className="form-error">{create.error.status}: {create.error.detail}</p>}
        {create.isSuccess && <p className="inline-note">Assignment saved.</p>}
      </div>

      <div className="card">
        <h2>Current assignments</h2>
        <QueryBoundary query={assignments} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Account</th>
                    <th>Execution mode</th>
                    <th>Risk profile</th>
                    <th>Enabled</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((a) => (
                    <tr key={a.strategy_id}>
                      <td>{a.strategy_id}</td>
                      <td className="mono">{a.account_id}</td>
                      <td>{a.execution_mode}</td>
                      <td>{a.risk_profile}</td>
                      <td>{a.enabled ? "yes" : "no"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>
      </div>
    </>
  );
}
