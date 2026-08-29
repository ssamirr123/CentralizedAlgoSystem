import { useAlgos, useStrategyHeartbeats } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { DAY_LOSS_LIMIT, STALE_MINUTES, TRADING_MODE } from "@/lib/config";
import { formatINR, isStale, pnlSign, relativeAge } from "@/lib/format";

export function RiskPage() {
  const hb = useStrategyHeartbeats();
  const algos = useAlgos();

  return (
    <>
      <PageHeader
        title="Risk"
        description="Derived risk view — day-loss breaches, error states and stale heartbeats. Display-only; no enforcement here."
      />

      <div className="grid cols-3" style={{ marginBottom: 16 }}>
        <div className="card stat">
          <span className="label">Trading mode</span>
          <span className={`value ${TRADING_MODE === "live" ? "neg" : "pos"}`}>{TRADING_MODE.toUpperCase()}</span>
          <span className="sub">this build</span>
        </div>
        <div className="card stat">
          <span className="label">Day-loss limit</span>
          <span className="value">{formatINR(DAY_LOSS_LIMIT)}</span>
          <span className="sub">mirrors backend DAY_LOSS_LIMIT</span>
        </div>
        <div className="card stat">
          <span className="label">Stale threshold</span>
          <span className="value">{STALE_MINUTES} min</span>
          <span className="sub">heartbeat age before flagged</span>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Day-loss check (from /strategies)</h2>
        <QueryBoundary query={hb} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Server</th>
                    <th>Status</th>
                    <th className="num">Day P&L</th>
                    <th className="num">Loss vs limit</th>
                    <th>Breach</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((h) => {
                    const loss = h.day_pnl < 0 ? Math.abs(h.day_pnl) : 0;
                    const breach = loss > DAY_LOSS_LIMIT;
                    return (
                      <tr key={`${h.strategy_name}|${h.server_name}`}>
                        <td>{h.strategy_name}</td>
                        <td className="mono">{h.server_name}</td>
                        <td>
                          <StatusBadge status={h.status} />
                        </td>
                        <td className={`num ${pnlSign(h.day_pnl)}`}>{formatINR(h.day_pnl)}</td>
                        <td className="num">
                          {loss === 0 ? "—" : `${Math.round((loss / DAY_LOSS_LIMIT) * 100)}%`}
                        </td>
                        <td>
                          {breach ? (
                            <span className="badge error">
                              <span className="dot" />
                              BREACH
                            </span>
                          ) : (
                            <span className="badge running">
                              <span className="dot" />
                              OK
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>
      </div>

      <div className="card">
        <h2>Health flags (from /api/algos)</h2>
        <QueryBoundary query={algos} empty={(d) => d.length === 0}>
          {(list) => {
            const flagged = list.filter((a) => a.status === "ERROR" || isStale(a.last_heartbeat));
            if (flagged.length === 0) return <div className="state">No strategies in ERROR or stale.</div>;
            return (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Strategy</th>
                      <th>Server</th>
                      <th>Status</th>
                      <th>Heartbeat age</th>
                      <th>Flag</th>
                    </tr>
                  </thead>
                  <tbody>
                    {flagged.map((a) => (
                      <tr key={`${a.algo_id}|${a.server_id}`}>
                        <td>{a.algo_id}</td>
                        <td className="mono">{a.server_id}</td>
                        <td>
                          <StatusBadge status={a.status} />
                        </td>
                        <td className="neg">{relativeAge(a.last_heartbeat)}</td>
                        <td>{a.status === "ERROR" ? "ERROR status" : "stale heartbeat"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          }}
        </QueryBoundary>
      </div>
    </>
  );
}
