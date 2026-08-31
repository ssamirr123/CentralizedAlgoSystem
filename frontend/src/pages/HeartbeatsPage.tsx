import { useAlgos } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge, StaleBadge } from "@/components/StatusBadge";
import { formatIST, isStale, relativeAge } from "@/lib/format";
import { STALE_MINUTES } from "@/lib/config";

export function HeartbeatsPage() {
  const algos = useAlgos();

  return (
    <>
      <PageHeader
        title="Heartbeats"
        description={`Freshness of the last heartbeat per strategy (POST /api/heartbeat). Stale = older than ${STALE_MINUTES} min.`}
        actions={
          <button className="sm" onClick={() => algos.refetch()}>
            Refresh
          </button>
        }
      />

      <div className="card">
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
    </>
  );
}
