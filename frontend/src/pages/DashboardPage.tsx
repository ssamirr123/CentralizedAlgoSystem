import { Link } from "react-router-dom";
import { useAlgos, useServers, usePnlToday, useStrategyHeartbeats } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Loading, ErrorState } from "@/components/States";
import { formatINR, isStale, pnlSign, relativeAge } from "@/lib/format";

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="card stat">
      <span className="label">{label}</span>
      <span className={`value ${tone ?? ""}`}>{value}</span>
      {sub && <span className="sub">{sub}</span>}
    </div>
  );
}

export function DashboardPage() {
  const algos = useAlgos();
  const servers = useServers();
  const pnl = usePnlToday();
  const hb = useStrategyHeartbeats();

  if (algos.isLoading || servers.isLoading) return <Loading />;
  if (algos.isError) return <ErrorState error={algos.error} onRetry={algos.refetch} />;

  const algoList = algos.data ?? [];
  const running = algoList.filter((a) => a.status === "RUNNING").length;
  const errored = algoList.filter((a) => a.status === "ERROR").length;
  const stale = algoList.filter((a) => isStale(a.last_heartbeat)).length;
  const serverList = servers.data ?? [];
  const pnlTotal = Object.values(pnl.data ?? {}).reduce((s, v) => s + v, 0);

  return (
    <>
      <PageHeader title="Dashboard" description="Fleet overview across all registered strategies and servers." />

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat label="Strategies" value={String(algoList.length)} sub={`${running} running`} />
        <Stat label="Servers" value={String(serverList.length)} sub={serverList.map((s) => s.status).join(", ") || "—"} />
        <Stat
          label="Errored / Stale"
          value={`${errored} / ${stale}`}
          tone={errored || stale ? "neg" : undefined}
          sub="ERROR status / heartbeat past threshold"
        />
        <Stat
          label="Day P&L (today)"
          value={pnl.isError ? "—" : formatINR(pnlTotal)}
          tone={pnlSign(pnlTotal)}
          sub="sum of DailyPnl rows for today (UTC)"
        />
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h2>Strategies</h2>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Server</th>
                  <th>Status</th>
                  <th>Last heartbeat</th>
                </tr>
              </thead>
              <tbody>
                {algoList.length === 0 && (
                  <tr>
                    <td colSpan={4} style={{ color: "var(--text-dim)" }}>
                      None registered. <Link to="/strategies">Strategies</Link>
                    </td>
                  </tr>
                )}
                {algoList.map((a) => (
                  <tr key={`${a.algo_id}|${a.server_id}`}>
                    <td>{a.algo_id}</td>
                    <td className="mono">{a.server_id}</td>
                    <td>
                      <StatusBadge status={a.status} />
                    </td>
                    <td className={isStale(a.last_heartbeat) ? "neg" : ""}>{relativeAge(a.last_heartbeat)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h2>Latest heartbeat snapshot</h2>
          {hb.isError ? (
            <div className="inline-note">Legacy /strategies feed unavailable.</div>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Status</th>
                    <th className="num">Day P&L</th>
                    <th className="num">MTM</th>
                    <th className="num">Trades</th>
                  </tr>
                </thead>
                <tbody>
                  {(hb.data ?? []).length === 0 && (
                    <tr>
                      <td colSpan={5} style={{ color: "var(--text-dim)" }}>
                        No heartbeats received yet.
                      </td>
                    </tr>
                  )}
                  {(hb.data ?? []).map((h) => (
                    <tr key={`${h.strategy_name}|${h.server_name}`}>
                      <td>{h.strategy_name}</td>
                      <td>
                        <StatusBadge status={h.status} />
                      </td>
                      <td className={`num ${pnlSign(h.day_pnl)}`}>{formatINR(h.day_pnl)}</td>
                      <td className={`num ${pnlSign(h.current_mtm)}`}>{formatINR(h.current_mtm)}</td>
                      <td className="num">{h.number_of_trades}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
