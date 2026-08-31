import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useServers, useAlgos, useServerStatus } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { formatIST, relativeAge, isStale } from "@/lib/format";

export function ServerDetailPage() {
  const { serverId = "" } = useParams();
  const navigate = useNavigate();
  const servers = useServers();
  const algos = useAlgos();
  const [live, setLive] = useState(false);
  const status = useServerStatus(serverId, live);

  const server = useMemo(
    () => (servers.data ?? []).find((s) => s.server_id === serverId),
    [servers.data, serverId],
  );
  const serverAlgos = useMemo(
    () => (algos.data ?? []).filter((a) => a.server_id === serverId),
    [algos.data, serverId],
  );

  return (
    <>
      <button className="detail-back" onClick={() => navigate("/servers")}>
        ← Servers
      </button>
      <PageHeader title={serverId} description="Server detail. Read-only view; controls are on the Servers list." />

      <QueryBoundary query={servers}>
        {() =>
          !server ? (
            <div className="state error">Server “{serverId}” is not registered.</div>
          ) : (
            <div className="grid cols-2">
              <div className="card">
                <h2>Overview</h2>
                <dl className="kv">
                  <dt>Server</dt>
                  <dd>{server.server_id}</dd>
                  <dt>EC2 instance</dt>
                  <dd className="mono">{server.ec2_instance_id}</dd>
                  <dt>Region</dt>
                  <dd>{server.region}</dd>
                  <dt>OS</dt>
                  <dd>{server.os}</dd>
                  <dt>Repo path</dt>
                  <dd className="mono">{server.repo_path}</dd>
                  <dt>Provisioning</dt>
                  <dd>
                    {server.provisioning_status}
                    {server.provisioning_message ? ` — ${server.provisioning_message}` : ""}
                  </dd>
                </dl>
              </div>

              <div className="card">
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <h2 style={{ margin: 0 }}>Health</h2>
                  <span style={{ flex: 1 }} />
                  <label style={{ display: "flex", gap: 6, alignItems: "center", margin: 0, fontSize: 12 }}>
                    <input
                      type="checkbox"
                      style={{ width: "auto" }}
                      checked={live}
                      onChange={(e) => setLive(e.target.checked)}
                    />
                    live check
                  </label>
                </div>
                <QueryBoundary query={status}>
                  {(d) => (
                    <dl className="kv" style={{ marginTop: 10 }}>
                      <dt>EC2 status</dt>
                      <dd>
                        <StatusBadge status={d.status} />
                      </dd>
                      <dt>SSM status</dt>
                      <dd>{d.ssm_status ?? "— (enable live check)"}</dd>
                      <dt>Live check healthy</dt>
                      <dd>{d.live_check_healthy === null ? "— (enable live check)" : String(d.live_check_healthy)}</dd>
                      <dt>Last heartbeat</dt>
                      <dd>{formatIST(d.last_heartbeat)}</dd>
                    </dl>
                  )}
                </QueryBoundary>
                <p className="inline-note" style={{ marginTop: 12 }}>
                  CPU / memory / disk are not exposed by the current backend API.
                </p>
              </div>

              <div className="card" style={{ gridColumn: "1 / -1" }}>
                <h2>Algorithms on this server</h2>
                {serverAlgos.length === 0 ? (
                  <div className="state">None registered.</div>
                ) : (
                  <div className="table-wrap">
                    <table className="data">
                      <thead>
                        <tr>
                          <th>Algorithm</th>
                          <th>Status</th>
                          <th>Enabled</th>
                          <th>Last heartbeat</th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        {serverAlgos.map((a) => (
                          <tr key={a.algo_id}>
                            <td>{a.algo_id}</td>
                            <td>
                              <StatusBadge status={a.status} />
                            </td>
                            <td>{a.enabled ? "yes" : "no"}</td>
                            <td className={isStale(a.last_heartbeat) ? "neg" : ""}>{relativeAge(a.last_heartbeat)}</td>
                            <td>
                              <button
                                className="sm"
                                onClick={() =>
                                  navigate(
                                    `/algorithms/${encodeURIComponent(a.algo_id)}?server=${encodeURIComponent(a.server_id)}`,
                                  )
                                }
                              >
                                Open
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )
        }
      </QueryBoundary>
    </>
  );
}
