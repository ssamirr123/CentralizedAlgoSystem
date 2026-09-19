import { useState } from "react";
import { useSendStrategyCommand, useStrategyLifecycle } from "@/api/hooks";
import { useAuth } from "@/auth/AuthContext";
import { ApiError } from "@/api/client";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import type { StrategyCommandResult } from "@/api/types";

// Phase 16.3/16.4: read-only lifecycle display plus two control-plane
// commands (START/STOP). Deliberately shows three SEPARATE columns
// (Lifecycle / Authorization / Execution) rather than one merged status --
// see trading/common/strategy_lifecycle.py's module docstring for why
// these must never be collapsed into a single value.
//
//     STRATEGY LIFECYCLE STATE  !=  LIVE AUTHORIZATION  !=  ORDER EXECUTION
//
// START/STOP here only flip the strategy's own in-memory lifecycle flag
// (trading/common/strategy_control.py) -- neither ever calls a broker,
// consumes a live authorization, or begins order execution. This page
// contains no BUY/SELL/PLACE ORDER/CLOSE POSITION/AUTHORIZE LIVE/GO LIVE
// control of any kind, and never will.
export function TccLifecyclePage() {
  const lifecycle = useStrategyLifecycle();
  const sendCommand = useSendStrategyCommand();
  const { hasPermission } = useAuth();
  const canStart = hasPermission("START");
  const canStop = hasPermission("STOP");

  const [lastResult, setLastResult] = useState<Record<string, StrategyCommandResult>>({});
  const [lastErrorFor, setLastErrorFor] = useState<string | null>(null);

  function send(strategyId: string, command: "START" | "STOP") {
    setLastErrorFor(null);
    sendCommand.mutate(
      { strategyId, command },
      {
        onSuccess: (result) => setLastResult((prev) => ({ ...prev, [strategyId]: result })),
        onError: () => setLastErrorFor(strategyId),
      },
    );
  }

  return (
    <>
      <PageHeader
        title="Strategy Lifecycle"
        description="Strategy control plane -- lifecycle control only. No order execution. START/STOP activate or deactivate a strategy's own configuration; they never place, modify, or cancel a broker order, and never grant live trading authorization."
      />

      <div className="card">
        <QueryBoundary query={lifecycle} empty={(d) => d.length === 0}>
          {(rows) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Account</th>
                    <th>Lifecycle</th>
                    <th>Authorization</th>
                    <th>Execution</th>
                    <th>Control</th>
                    <th>Last transition</th>
                    <th>Last error</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const pending = sendCommand.isPending && sendCommand.variables?.strategyId === r.strategy_id;
                    const result = lastResult[r.strategy_id];
                    return (
                      <tr key={r.strategy_id}>
                        <td>{r.strategy_id}</td>
                        <td className="mono">{r.account_id ?? "—"}</td>
                        <td>
                          <span className={`badge lifecycle-${r.lifecycle_state.toLowerCase()}`}>{r.lifecycle_state}</span>
                        </td>
                        <td>{r.account_authorization_state ?? "—"}{r.live_authorized ? " (LIVE_AUTHORIZED)" : ""}</td>
                        <td>{r.execution_active ? "ACTIVE" : "INACTIVE"}</td>
                        <td>
                          <div className="filters" style={{ gap: 6 }}>
                            <button
                              className="sm"
                              disabled={!canStart || pending || r.lifecycle_state === "RUNNING"}
                              title={canStart ? undefined : "Requires START permission"}
                              onClick={() => send(r.strategy_id, "START")}
                            >
                              {pending && sendCommand.variables?.command === "START" ? "Starting…" : "Start"}
                            </button>
                            <button
                              className="sm"
                              disabled={!canStop || pending || r.lifecycle_state === "STOPPED"}
                              title={canStop ? undefined : "Requires STOP permission"}
                              onClick={() => send(r.strategy_id, "STOP")}
                            >
                              {pending && sendCommand.variables?.command === "STOP" ? "Stopping…" : "Stop"}
                            </button>
                          </div>
                          {result && (
                            <p className="inline-note" data-testid={`command-result-${r.strategy_id}`}>
                              {result.command} {result.result}
                              {result.result === "REJECTED" || result.result === "FAILED" ? `: ${result.reason}` : ""}
                            </p>
                          )}
                          {lastErrorFor === r.strategy_id && sendCommand.error instanceof ApiError && (
                            <p className="form-error">{sendCommand.error.status}: {sendCommand.error.detail}</p>
                          )}
                        </td>
                        <td className="mono">{r.last_transition_at || "—"}</td>
                        <td>{r.last_error || "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>
      </div>
    </>
  );
}
