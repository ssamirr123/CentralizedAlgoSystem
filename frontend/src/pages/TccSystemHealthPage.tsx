import { useExecutionSystemStatus } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { Loading, ErrorState } from "@/components/States";
import { KillSwitchBanner } from "@/components/KillSwitchBanner";
import { brokerLabel } from "@/lib/format";

const STATUS_ORDER = ["running", "shadow", "enabled", "starting", "stopped", "disabled", "error"] as const;

export function TccSystemHealthPage() {
  const status = useExecutionSystemStatus();

  if (status.isLoading) return <Loading />;
  if (status.isError) return <ErrorState error={status.error} onRetry={status.refetch} />;
  const data = status.data!;

  return (
    <>
      <PageHeader
        title="System Health"
        description="Execution-framework health (Phase 11) — kill switch, strategy lifecycle counts, account/broker availability. Distinct from the legacy EC2/infra System Health page."
      />

      <KillSwitchBanner engaged={data.kill_switch_engaged} />

      <div className="grid cols-3" style={{ marginBottom: 16 }}>
        <div className="card stat">
          <span className="label">Accounts</span>
          <span className="value">{data.account_count}</span>
        </div>
        <div className="card stat">
          <span className="label">Assigned strategies</span>
          <span className="value">{data.assigned_strategy_count}</span>
        </div>
        <div className="card stat">
          <span className="label">Kill switch</span>
          <span className={`value ${data.kill_switch_engaged ? "neg" : "pos"}`}>
            {data.kill_switch_engaged ? "ENGAGED" : "CLEAR"}
          </span>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Strategies by status</h2>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Status</th>
                <th className="num">Count</th>
              </tr>
            </thead>
            <tbody>
              {STATUS_ORDER.filter((s) => data.strategy_counts_by_status[s] != null).map((s) => (
                <tr key={s}>
                  <td style={{ textTransform: "uppercase" }}>{s}</td>
                  <td className="num">{data.strategy_counts_by_status[s]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h2>Broker availability</h2>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Broker</th>
                <th>Available</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.broker_availability).map(([brokerId, available]) => (
                <tr key={brokerId}>
                  <td>{brokerLabel(brokerId)}</td>
                  <td>
                    {available ? (
                      <span className="badge running">
                        <span className="dot" />
                        AVAILABLE
                      </span>
                    ) : (
                      <span className="badge error">
                        <span className="dot" />
                        UNAVAILABLE
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
