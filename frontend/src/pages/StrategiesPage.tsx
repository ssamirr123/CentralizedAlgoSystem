import { useAlgos } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { formatIST, relativeAge, isStale } from "@/lib/format";

export function StrategiesPage() {
  const algos = useAlgos();

  return (
    <>
      <PageHeader
        title="Strategies"
        description="Every registered strategy across all servers (GET /api/algos)."
        actions={
          <button className="sm" onClick={() => algos.refetch()}>
            Refresh
          </button>
        }
      />

      <QueryBoundary query={algos} empty={(d) => d.length === 0}>
        {(list) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Server</th>
                  <th>Status</th>
                  <th>Enabled</th>
                  <th>Script path</th>
                  <th>Last heartbeat</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {list.map((a) => (
                  <tr key={`${a.algo_id}|${a.server_id}`}>
                    <td>{a.algo_id}</td>
                    <td className="mono">{a.server_id}</td>
                    <td>
                      <StatusBadge status={a.status} />
                    </td>
                    <td>{a.enabled ? "yes" : "no"}</td>
                    <td className="mono">{a.script_path}</td>
                    <td className={isStale(a.last_heartbeat) ? "neg" : ""}>{relativeAge(a.last_heartbeat)}</td>
                    <td>{formatIST(a.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryBoundary>

      <div className="inline-note" style={{ marginTop: 14 }}>
        This is the read-only roster. Add / edit / delete and start / stop / restart are on the{" "}
        <strong>Algorithms</strong> screen; the <strong>Commands</strong> screen also does process control.
      </div>
    </>
  );
}
