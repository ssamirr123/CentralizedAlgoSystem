import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  useCreateExecutionAssignment,
  useExecutionAccounts,
  useExecutionAssignments,
  useExecutionModes,
  useExecutionPnl,
  useExecutionStrategies,
  useStartExecutionStrategy,
  useStopExecutionStrategy,
} from "@/api/hooks";
import type { ExecutionAccount, ExecutionAssignment, ExecutionModeValue } from "@/api/types";
import { ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { ExecutionModeSelect } from "@/components/ExecutionModeSelect";
import { brokerLabel, formatINR, pnlSign } from "@/lib/format";

export function TccStrategiesPage() {
  const { hasPermission } = useAuth();
  const canStart = hasPermission("START");
  const canStop = hasPermission("STOP");
  const canAssign = hasPermission("TRADING_CONTROL");

  const strategies = useExecutionStrategies();
  const accounts = useExecutionAccounts();
  const assignments = useExecutionAssignments();
  const pnl = useExecutionPnl();
  const modes = useExecutionModes();

  const start = useStartExecutionStrategy();
  const stop = useStopExecutionStrategy();
  const assign = useCreateExecutionAssignment();

  const [assignTarget, setAssignTarget] = useState<string | null>(null);
  const [accountId, setAccountId] = useState("");
  const [executionMode, setExecutionMode] = useState<ExecutionModeValue | "">("");

  const assignmentByStrategy = useMemo(() => {
    const m = new Map<string, ExecutionAssignment>();
    for (const a of assignments.data ?? []) m.set(a.strategy_id, a);
    return m;
  }, [assignments.data]);

  const accountById = useMemo(() => {
    const m = new Map<string, ExecutionAccount>();
    for (const a of accounts.data ?? []) m.set(a.account_id, a);
    return m;
  }, [accounts.data]);

  function openAssign(strategyId: string) {
    const existing = assignmentByStrategy.get(strategyId);
    setAccountId(existing?.account_id ?? "");
    setExecutionMode("");
    setAssignTarget(strategyId);
    assign.reset();
  }

  function submitAssign() {
    if (!assignTarget || !accountId) return;
    assign.mutate(
      { strategy_id: assignTarget, account_id: accountId, execution_mode: executionMode || null },
      { onSuccess: () => setAssignTarget(null) },
    );
  }

  return (
    <>
      <PageHeader
        title="Strategies"
        description="DoubleStraddelAlgo, CombinedVwapNifty, Vwap_Algo_Nifty_hedge — registered in the Phase 10 StrategyRegistry. Start/Stop only flip this process's own lifecycle state; no order is ever placed from here."
      />

      <QueryBoundary query={strategies} empty={(d) => d.length === 0}>
        {(list) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Status</th>
                  <th>Account</th>
                  <th>Broker</th>
                  <th>Mode</th>
                  <th className="num">P&amp;L</th>
                  <th>Position</th>
                  <th>Last heartbeat</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => {
                  const assignment = assignmentByStrategy.get(s.strategy_id);
                  const account = assignment ? accountById.get(assignment.account_id) : undefined;
                  const running = s.status === "running" || s.status === "shadow";
                  const strategyPnl = pnl.data?.per_strategy?.[s.strategy_id];
                  return (
                    <tr key={s.strategy_id}>
                      <td>{s.strategy_id}</td>
                      <td>
                        <StatusBadge status={s.status} />
                      </td>
                      <td>{account ? account.account_id : <span className="sub">unassigned</span>}</td>
                      <td>{account ? brokerLabel(account.broker_id) : "—"}</td>
                      <td>{s.execution_mode}</td>
                      <td className={`num ${pnlSign(strategyPnl)}`}>
                        {strategyPnl == null ? "—" : formatINR(strategyPnl)}
                      </td>
                      <td className="sub" title="No shadow/live order flow is wired to a strategy yet (Phase 10/11 scope)">
                        —
                      </td>
                      <td className="sub" title="Heartbeats are a legacy-telemetry concept, not part of this execution framework yet">
                        —
                      </td>
                      <td>
                        <div className="ec-actions">
                          <button
                            className="sm primary"
                            disabled={!canStart || running || start.isPending}
                            title={canStart ? undefined : "Requires START"}
                            onClick={() => start.mutate(s.strategy_id)}
                          >
                            Start
                          </button>
                          <button
                            className="sm danger"
                            disabled={!canStop || !running || stop.isPending}
                            title={canStop ? undefined : "Requires STOP"}
                            onClick={() => stop.mutate(s.strategy_id)}
                          >
                            Stop
                          </button>
                          <button
                            className="sm"
                            disabled={!canAssign}
                            title={canAssign ? undefined : "Requires TRADING_CONTROL"}
                            onClick={() => openAssign(s.strategy_id)}
                          >
                            Assign
                          </button>
                          <Link className="sm ghost" to="/execution/risk">
                            Risk
                          </Link>
                          <Link className="sm ghost" to="/execution/orders">
                            Orders
                          </Link>
                          <Link className="sm ghost" to="/execution/positions">
                            Positions
                          </Link>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </QueryBoundary>

      {assignTarget && (
        <div className="modal-overlay" role="dialog" aria-modal="true" onClick={() => setAssignTarget(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Assign {assignTarget}</h3>
            <div className="field">
              <label>Account</label>
              <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                <option value="">Select an account…</option>
                {(accounts.data ?? []).map((a) => (
                  <option key={a.account_id} value={a.account_id}>
                    {a.account_id} — {brokerLabel(a.broker_id)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Execution mode</label>
              <ExecutionModeSelect modes={modes.data} value={executionMode} onChange={setExecutionMode} />
              <span className="sub">Leave as "Account default" to adopt the account's own mode. LIVE is disabled by the frontend safety gate.</span>
            </div>
            {assign.error instanceof ApiError && <p className="form-error">{assign.error.status}: {assign.error.detail}</p>}
            <div className="actions">
              <button className="ghost" onClick={() => setAssignTarget(null)} disabled={assign.isPending}>
                Cancel
              </button>
              <button className="primary" onClick={submitAssign} disabled={!accountId || assign.isPending}>
                {assign.isPending ? "Working…" : "Save assignment"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
