import { useAlgos, useStrategyHeartbeats } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge, StaleBadge } from "@/components/StatusBadge";
import { formatINR, formatIST, isStale, pnlSign, relativeAge } from "@/lib/format";
import { STALE_MINUTES } from "@/lib/config";

export function HeartbeatsPage() {
  const algos = useAlgos();
  const legacy = useStrategyHeartbeats();

  return (
    <>
      <PageHeader
        title="Heartbeats"
        description={`Freshness of the last heartbeat per strategy. Stale = older than ${STALE_MINUTES} min.`}
        actions={
          <button
            className="sm"
            onClick={() => {
              algos.refetch();
              legacy.refetch();
            }}
          >
            Refresh
          </button>
        }
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Registered strategies — last heartbeat</h2>
        <QueryBoundary query={algos} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Server</th>
                    <th>Reported status</th>
                    <th>Freshness</th>
                    <th>Age</th>
                    <th>Last heartbeat (IST)</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((a) => {
                    const stale = isStale(a.last_heartbeat);
                    return (
                      <tr key={`${a.algo_id}|${a.server_id}`}>
                        <td>{a.algo_id}</td>
                        <td className="mono">{a.server_id}</td>
                        <td>
                          <StatusBadge status={a.status} />
                        </td>
                        <td>
                          <StaleBadge stale={stale} />
                        </td>
                        <td className={stale ? "neg" : ""}>{relativeAge(a.last_heartbeat)}</td>
                        <td>{formatIST(a.last_heartbeat)}</td>
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
        <h2>Legacy heartbeat feed (GET /strategies)</h2>
        <p style={{ color: "var(--text-dim)", marginTop: 0, fontSize: 12.5 }}>
          Richer per-heartbeat snapshot (MTM, day P&L, trade count) still posted by strategy processes.
        </p>
        <QueryBoundary query={legacy} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Server</th>
                    <th>Status</th>
                    <th className="num">Day P&L</th>
                    <th className="num">MTM</th>
                    <th className="num">Trades</th>
                    <th>Last update</th>
                    <th>Received</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((h) => (
                    <tr key={`${h.strategy_name}|${h.server_name}`}>
                      <td>{h.strategy_name}</td>
                      <td className="mono">{h.server_name}</td>
                      <td>
                        <StatusBadge status={h.status} />
                      </td>
                      <td className={`num ${pnlSign(h.day_pnl)}`}>{formatINR(h.day_pnl)}</td>
                      <td className={`num ${pnlSign(h.current_mtm)}`}>{formatINR(h.current_mtm)}</td>
                      <td className="num">{h.number_of_trades}</td>
                      <td className={isStale(h.last_update_time) ? "neg" : ""}>{relativeAge(h.last_update_time)}</td>
                      <td>{relativeAge(h.received_at)}</td>
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
