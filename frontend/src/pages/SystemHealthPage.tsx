import { useState } from "react";
import { useHealth, useServers } from "@/api/hooks";
import { getServerStatus } from "@/api/endpoints";
import type { ServerStatusResponse } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { formatIST } from "@/lib/format";
import { authStore } from "@/auth/authStore";

export function SystemHealthPage() {
  const health = useHealth();
  const servers = useServers();
  const [checks, setChecks] = useState<Record<string, ServerStatusResponse | { error: string } | "loading">>({});

  async function liveCheck(serverId: string) {
    setChecks((c) => ({ ...c, [serverId]: "loading" }));
    try {
      const res = await getServerStatus(serverId, true);
      setChecks((c) => ({ ...c, [serverId]: res }));
    } catch (e) {
      setChecks((c) => ({ ...c, [serverId]: { error: e instanceof Error ? e.message : "failed" } }));
    }
  }

  const dbOk = health.data?.database === "connected";

  return (
    <>
      <PageHeader
        title="System Health"
        description="Backend liveness (GET /api/health) and per-server EC2/SSM checks (check_ec2_health via Lambda)."
        actions={
          <button className="sm" onClick={() => health.refetch()}>
            Refresh
          </button>
        }
      />

      <div className="grid cols-3" style={{ marginBottom: 16 }}>
        <div className="card stat">
          <span className="label">API</span>
          <span className={`value ${health.isError ? "neg" : health.data?.status === "ok" ? "pos" : "neg"}`}>
            {health.isError ? "UNREACHABLE" : (health.data?.status ?? "…").toUpperCase()}
          </span>
          <span className="sub">{health.data?.service ?? "centralized-algo-backend"}</span>
        </div>
        <div className="card stat">
          <span className="label">Database</span>
          <span className={`value ${dbOk ? "pos" : "neg"}`}>{dbOk ? "CONNECTED" : "DOWN"}</span>
          <span className="sub">{health.data?.database ?? "—"}</span>
        </div>
        <div className="card stat">
          <span className="label">API endpoint</span>
          <span className="value" style={{ fontSize: 14 }}>
            {authStore.get().baseUrl || "same-origin /api"}
          </span>
          <span className="sub">{health.data ? `checked ${formatIST(health.data.timestamp)}` : "—"}</span>
        </div>
      </div>

      <div className="card">
        <h2>Servers</h2>
        <QueryBoundary query={servers} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Server</th>
                    <th>Instance</th>
                    <th>Cached EC2 status</th>
                    <th>Provisioning</th>
                    <th>Live EC2</th>
                    <th>SSM</th>
                    <th>Healthy</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((s) => {
                    const c = checks[s.server_id];
                    const isObj = typeof c === "object" && c !== null;
                    const res = isObj && !("error" in c) ? (c as ServerStatusResponse) : null;
                    const errored = isObj && "error" in c;
                    return (
                      <tr key={s.server_id}>
                        <td>{s.server_id}</td>
                        <td className="mono">{s.ec2_instance_id}</td>
                        <td>{s.status}</td>
                        <td>{s.provisioning_status}</td>
                        <td>{res ? res.status : c === "loading" ? "…" : "—"}</td>
                        <td>{res ? (res.ssm_status ?? "—") : errored ? "err" : "—"}</td>
                        <td className={res?.live_check_healthy ? "pos" : res ? "neg" : ""}>
                          {res ? String(res.live_check_healthy) : "—"}
                        </td>
                        <td>
                          <button className="sm ghost" onClick={() => liveCheck(s.server_id)} disabled={c === "loading"}>
                            Live check
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>
        <div className="inline-note" style={{ marginTop: 12 }}>
          A failed live check means “can’t verify right now”, not “definitely unhealthy” — the backend degrades to the
          cached EC2 status in that case.
        </div>
      </div>
    </>
  );
}
