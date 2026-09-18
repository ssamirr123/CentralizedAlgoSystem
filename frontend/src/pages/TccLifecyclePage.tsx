import { useStrategyLifecycle } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";

// Phase 16.3: read-only. Deliberately shows three SEPARATE columns
// (Lifecycle / Authorization / Execution) rather than one merged status --
// see trading/common/strategy_lifecycle.py's module docstring for why
// these must never be collapsed into a single value.
//
//     STRATEGY LIFECYCLE STATE  !=  LIVE AUTHORIZATION  !=  ORDER EXECUTION
//
// This page contains no start/stop/authorize/order control of any kind.
export function TccLifecyclePage() {
  const lifecycle = useStrategyLifecycle();

  return (
    <>
      <PageHeader
        title="Strategy Lifecycle"
        description="Read-only lifecycle state, derived from existing strategy status and assignment readiness (Phase 16.2). Lifecycle state, account authorization, and order execution are three separate, independently-tracked facts -- none of them implies the others."
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
                    <th>Last transition</th>
                    <th>Last error</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.strategy_id}>
                      <td>{r.strategy_id}</td>
                      <td className="mono">{r.account_id ?? "—"}</td>
                      <td>
                        <span className={`badge lifecycle-${r.lifecycle_state.toLowerCase()}`}>{r.lifecycle_state}</span>
                      </td>
                      <td>{r.account_authorization_state ?? "—"}{r.live_authorized ? " (LIVE_AUTHORIZED)" : ""}</td>
                      <td>{r.execution_active ? "ACTIVE" : "INACTIVE"}</td>
                      <td className="mono">{r.last_transition_at || "—"}</td>
                      <td>{r.last_error || "—"}</td>
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
