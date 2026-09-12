import { useExecutionPnl } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { Loading, ErrorState } from "@/components/States";
import { formatINR, pnlSign } from "@/lib/format";

export function TccPnlPage() {
  const pnl = useExecutionPnl();

  if (pnl.isLoading) return <Loading />;
  if (pnl.isError) return <ErrorState error={pnl.error} onRetry={pnl.refetch} />;
  const data = pnl.data;

  return (
    <>
      <PageHeader
        title="P&L"
        description="Realized/unrealized P&L from the execution framework's own shadow/live order flow — separate from the legacy per-algo P&L pages."
      />

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <div className="card stat">
          <span className="label">Total realized</span>
          <span className={`value ${pnlSign(data?.total_realized)}`}>{formatINR(data?.total_realized ?? 0)}</span>
        </div>
        <div className="card stat">
          <span className="label">Total unrealized</span>
          <span className={`value ${pnlSign(data?.total_unrealized)}`}>{formatINR(data?.total_unrealized ?? 0)}</span>
        </div>
      </div>

      <div className="card">
        <h2>Per strategy</h2>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Strategy</th>
                <th className="num">P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data?.per_strategy ?? {}).map(([strategyId, value]) => (
                <tr key={strategyId}>
                  <td>{strategyId}</td>
                  <td className={`num ${pnlSign(value)}`}>{formatINR(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="sub" style={{ marginTop: 12 }}>
          All zero today — no strategy generates order intents yet (Phase 10's deliberate scope).
        </p>
      </div>
    </>
  );
}
