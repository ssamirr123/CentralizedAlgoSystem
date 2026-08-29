import { useState } from "react";
import { useServers, useServerStatus } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { formatIST, relativeAge } from "@/lib/format";

export function ServersPage() {
  const servers = useServers();
  const [selected, setSelected] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const detail = useServerStatus(selected, live);

  return (
    <>
      <PageHeader
        title="Servers"
        description="EC2 trading servers registered with the control plane (GET /api/servers)."
        actions={
          <button className="sm" onClick={() => servers.refetch()}>
            Refresh
          </button>
        }
      />

      <QueryBoundary query={servers} empty={(d) => d.length === 0}>
        {(list) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Server</th>
                  <th>Instance</th>
                  <th>Region</th>
                  <th>OS</th>
                  <th>EC2 status</th>
                  <th>Provisioning</th>
                  <th>Last heartbeat</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => (
                  <tr key={s.server_id}>
                    <td>{s.server_id}</td>
                    <td className="mono">{s.ec2_instance_id}</td>
                    <td>{s.region}</td>
                    <td>{s.os}</td>
                    <td>{s.status}</td>
                    <td>
                      {s.provisioning_status}
                      {s.provisioning_message ? (
                        <span style={{ color: "var(--text-faint)" }}> — {s.provisioning_message}</span>
                      ) : null}
                    </td>
                    <td>{relativeAge(s.last_heartbeat)}</td>
                    <td>
                      <button
                        className="sm ghost"
                        onClick={() => {
                          setSelected(s.server_id);
                          setLive(false);
                        }}
                      >
                        Inspect
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryBoundary>

      {selected && (
        <div className="card" style={{ marginTop: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>{selected}</h2>
            <div style={{ flex: 1 }} />
            <label style={{ display: "flex", gap: 6, alignItems: "center", margin: 0 }}>
              <input
                type="checkbox"
                style={{ width: "auto" }}
                checked={live}
                onChange={(e) => setLive(e.target.checked)}
              />
              live check (calls check_ec2_health via Lambda)
            </label>
            <button className="sm ghost" onClick={() => setSelected(null)}>
              Close
            </button>
          </div>
          <QueryBoundary query={detail}>
            {(d) => (
              <dl className="kv">
                <dt>EC2 instance</dt>
                <dd className="mono">{d.ec2_instance_id}</dd>
                <dt>Region</dt>
                <dd>{d.region}</dd>
                <dt>EC2 status</dt>
                <dd>{d.status}</dd>
                <dt>SSM status</dt>
                <dd>{d.ssm_status ?? "— (enable live check)"}</dd>
                <dt>Live check healthy</dt>
                <dd>{d.live_check_healthy === null ? "— (enable live check)" : String(d.live_check_healthy)}</dd>
                <dt>Last heartbeat</dt>
                <dd>{formatIST(d.last_heartbeat)}</dd>
              </dl>
            )}
          </QueryBoundary>
        </div>
      )}
    </>
  );
}
