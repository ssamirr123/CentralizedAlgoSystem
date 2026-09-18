import { Link } from "react-router-dom";
import { useExecutionStrategies, useExecutionSystemStatus } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary, Loading, ErrorState } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { KillSwitchBanner } from "@/components/KillSwitchBanner";

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="card stat">
      <span className="label">{label}</span>
      <span className={`value ${tone ?? ""}`}>{value}</span>
      {sub && <span className="sub">{sub}</span>}
    </div>
  );
}

export function TccOverviewPage() {
  const status = useExecutionSystemStatus();
  const strategies = useExecutionStrategies();

  if (status.isLoading || strategies.isLoading) return <Loading />;
  if (status.isError) return <ErrorState error={status.error} onRetry={status.refetch} />;

  const s = status.data;
  const activeCount = (s?.strategy_counts_by_status.running ?? 0) + (s?.strategy_counts_by_status.shadow ?? 0);
  const brokersUnavailable = Object.values(s?.broker_availability ?? {}).filter((v) => !v).length;

  return (
    <>
      <PageHeader
        title="Execution Overview"
        description="Broker-agnostic execution framework (Phases 1-11) — strategies, accounts, assignments and risk, distinct from the legacy algorithm/server telemetry pages."
      />

      <KillSwitchBanner engaged={s?.kill_switch_engaged ?? false} />

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat label="Strategies" value={String(strategies.data?.length ?? 0)} sub={`${activeCount} active (running/shadow)`} />
        <Stat label="Accounts" value={String(s?.account_count ?? 0)} sub="registered trading accounts" />
        <Stat
          label="Brokers unavailable"
          value={String(brokersUnavailable)}
          tone={brokersUnavailable > 0 ? "neg" : undefined}
          sub="see the Brokers page"
        />
        <Stat
          label="Assigned strategies"
          value={String(s?.assigned_strategy_count ?? 0)}
          sub="strategy → account routing set"
        />
      </div>

      <div className="card">
        <h2>Strategies</h2>
        <QueryBoundary query={strategies} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Status</th>
                    <th>Execution mode</th>
                    <th className="num">Intents generated</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((st) => (
                    <tr key={st.strategy_id}>
                      <td>{st.strategy_id}</td>
                      <td>
                        <StatusBadge status={st.status} />
                      </td>
                      <td>{st.execution_mode}</td>
                      <td className="num">{st.intents_generated}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>
        <div style={{ marginTop: 12 }}>
          <Link to="/execution/strategies">Manage strategies →</Link>
        </div>
      </div>
    </>
  );
}
