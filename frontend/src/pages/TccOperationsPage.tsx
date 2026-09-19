import { useOperationsAudit, useOperationsSummary } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/States";
import type { OperationalAlert, OperationsStrategyHealth, OperationsWorkerHealth } from "@/api/types";

// Phase 16.11: a single, read-only console over every authoritative
// component the Trading Control Center already exposes (workers,
// strategies, accounts, portfolio risk, kill switch, deployment info,
// alerts). This page NEVER places/modifies/cancels an order, never
// authorizes live trading, never starts/stops a strategy, and never sets
// a risk limit -- it only observes. See trading/common/operations_snapshot
// .py's own module docstring: reading this page's data can raise/resolve
// a diagnostic alert as a side effect, but that is presentation
// bookkeeping, never a trading-state mutation.
function fmt(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function ageLabel(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  return `${Math.round(seconds / 60)}m ago`;
}

function SeverityBadge({ severity }: { severity: string }) {
  return <span className={`badge severity-${severity.toLowerCase()}`}>{severity}</span>;
}

function WorkerRow({ w }: { w: OperationsWorkerHealth }) {
  return (
    <tr>
      <td className="mono">{w.worker_id}</td>
      <td>{w.name}</td>
      <td><span className={`badge worker-${w.status.toLowerCase()}`}>{w.status}</span></td>
      <td>{ageLabel(w.heartbeat_age_seconds)}</td>
      <td>{w.heartbeat_timeout_seconds.toFixed(0)}s</td>
      <td>{w.assigned_strategy_ids.join(", ") || "—"}</td>
      <td className="mono">{w.git_sha || "—"}{w.version_mismatch && <span className="badge severity-warning" style={{ marginLeft: 6 }}>MISMATCH</span>}</td>
      <td className="mono">{w.host_identity || "—"}</td>
    </tr>
  );
}

function StrategyRow({ s }: { s: OperationsStrategyHealth }) {
  return (
    <tr>
      <td>{s.strategy_id}</td>
      <td><span className={`badge lifecycle-${s.lifecycle_state.toLowerCase()}`}>{s.lifecycle_state}</span></td>
      <td><span className={`badge runtime-${s.runtime_state.toLowerCase()}`}>{s.runtime_state}</span></td>
      <td>{s.account_authorization_state ?? "—"}</td>
      <td>{s.execution_active ? "SHADOW" : "INACTIVE"}</td>
      <td className="mono">{s.worker_id ?? "—"}</td>
      <td className="mono">{s.account_id ?? "—"}</td>
      <td>{s.market_data_status || "—"}</td>
      <td>{s.last_error ? <span className="form-error">{s.last_error}</span> : "—"}</td>
    </tr>
  );
}

function AlertRow({ a }: { a: OperationalAlert }) {
  return (
    <tr>
      <td><SeverityBadge severity={a.severity} /></td>
      <td>{a.category}</td>
      <td className="mono">{a.source_type}:{a.source_id}</td>
      <td>{a.code}</td>
      <td>{a.message}</td>
      <td className="mono">{a.raised_at}</td>
    </tr>
  );
}

export function TccOperationsPage() {
  const summary = useOperationsSummary();
  const audit = useOperationsAudit({ limit: 25 });

  return (
    <>
      <PageHeader
        title="Operations"
        description="Read-only operational console over the entire distributed trading platform -- worker health, strategy health, portfolio risk, and alerts. Observes existing authoritative state only; contains no BUY/SELL/PLACE ORDER/AUTHORIZE LIVE/GO LIVE control of any kind."
      />

      {summary.isLoading && <Loading />}

      {summary.isError && (
        <div className="card" style={{ borderColor: "var(--danger, #b42318)" }}>
          <h2>OPERATIONS DATA UNAVAILABLE</h2>
          <p>
            The dashboard itself has stopped receiving updates from the TCC backend -- this is NOT the same as the
            backend reporting a problem. Do not treat any previously-shown values as current.
          </p>
          <p className="sub">
            Last successful refresh:{" "}
            {summary.dataUpdatedAt ? new Date(summary.dataUpdatedAt).toLocaleString() : "never"}
          </p>
        </div>
      )}

      {summary.data && (
        <>
          <div className="card" style={{ display: "flex", gap: 24, flexWrap: "wrap", alignItems: "center" }}>
            <div>
              <div className="sub">System</div>
              <strong>{summary.data.system.ready ? "READY" : "NOT READY"}</strong>
            </div>
            <div>
              <div className="sub">Kill switch</div>
              <strong className={summary.data.safety.kill_switch_engaged ? "form-error" : ""}>
                {summary.data.safety.kill_switch_engaged ? "ENGAGED" : "DISENGAGED"}
              </strong>
            </div>
            <div>
              <div className="sub">Execution mode</div>
              <strong>{summary.data.safety.execution_mode_banner}</strong>
            </div>
            <div>
              <div className="sub">Live trading</div>
              <strong>{summary.data.safety.live_trading_disabled ? "DISABLED" : "ENABLED"}</strong>
            </div>
            <div>
              <div className="sub">Active alerts</div>
              <strong>
                {summary.data.active_alerts.filter((a) => a.severity === "CRITICAL").length} critical /{" "}
                {summary.data.active_alerts.filter((a) => a.severity === "WARNING").length} warning
              </strong>
            </div>
            <div>
              <div className="sub">Deployment</div>
              <strong className="mono">
                {summary.data.system.environment} · {summary.data.system.app_version} · {summary.data.system.git_sha}
              </strong>
            </div>
            <div>
              <div className="sub">Uptime</div>
              <strong>{fmt(summary.data.system.uptime_seconds / 60)} min</strong>
            </div>
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <h2>Workers</h2>
            {summary.data.workers.length === 0 ? (
              <p className="sub">No workers registered.</p>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Worker</th><th>Name</th><th>Status</th><th>Heartbeat</th><th>Timeout</th>
                      <th>Strategies</th><th>Git SHA</th><th>Host</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.data.workers.map((w) => <WorkerRow key={w.worker_id} w={w} />)}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <h2>Strategies</h2>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th><th>Lifecycle</th><th>Runtime</th><th>Authorization</th><th>Execution</th>
                    <th>Worker</th><th>Account</th><th>Market data</th><th>Last error</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.data.strategies.map((s) => <StrategyRow key={s.strategy_id} s={s} />)}
                </tbody>
              </table>
            </div>
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <h2>Portfolio risk</h2>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr><th>Daily P&amp;L</th><th>Gross exposure</th><th>Open orders</th><th>Orders today</th><th>Status</th></tr>
                </thead>
                <tbody>
                  <tr>
                    <td className="num">{fmt(summary.data.portfolio_risk.daily_pnl)}</td>
                    <td className="num">{fmt(summary.data.portfolio_risk.gross_exposure)}</td>
                    <td className="num">{summary.data.portfolio_risk.open_orders}</td>
                    <td className="num">{summary.data.portfolio_risk.orders_today}</td>
                    <td><span className="badge">{summary.data.portfolio_risk.risk_status}</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p className="sub" style={{ marginTop: 8 }}>
              Exposure is notional/quantity based, NOT delta-adjusted. Unrealized P&amp;L is UNSUPPORTED -- only
              realized P&amp;L from actual shadow fills backs this figure.
            </p>
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <h2>Recent activity</h2>
            {audit.data && audit.data.length > 0 ? (
              <div className="table-wrap">
                <table className="data">
                  <thead><tr><th>Time</th><th>Event</th><th>Strategy</th></tr></thead>
                  <tbody>
                    {audit.data.map((r) => (
                      <tr key={r.seq}>
                        <td className="mono">{r.timestamp}</td>
                        <td>{r.event_type}</td>
                        <td>{r.strategy_id || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="sub">No activity recorded yet.</p>
            )}
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <h2>Active alerts</h2>
            {summary.data.active_alerts.length === 0 ? (
              <p className="sub">No active alerts.</p>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr><th>Severity</th><th>Category</th><th>Source</th><th>Code</th><th>Message</th><th>Raised at</th></tr>
                  </thead>
                  <tbody>
                    {summary.data.active_alerts.map((a) => <AlertRow key={a.alert_id} a={a} />)}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </>
  );
}
