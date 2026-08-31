import { useMemo } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAlgos, usePnlHistory, usePositions, useTrades, useLogs } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { TRADING_MODE } from "@/lib/config";
import { formatIST, formatINR, relativeAge, pnlSign } from "@/lib/format";

export function AlgoDetailPage() {
  const { algoId = "" } = useParams();
  const [sp] = useSearchParams();
  const serverId = sp.get("server") ?? "";
  const navigate = useNavigate();

  const algos = useAlgos();
  const entry = useMemo(
    () => (algos.data ?? []).find((a) => a.algo_id === algoId && a.server_id === serverId),
    [algos.data, algoId, serverId],
  );

  const pnl = usePnlHistory(algoId || null, serverId || null);
  const positions = usePositions(algoId || null, serverId || null);
  const trades = useTrades(algoId || null, serverId || null, 25);
  const logs = useLogs(algoId && serverId ? { algo_id: algoId, server_id: serverId, limit: 25 } : null);

  const todayPnl = pnl.data?.[0];

  return (
    <>
      <button className="detail-back" onClick={() => navigate("/algorithms")}>
        ← Algorithms
      </button>
      <PageHeader title={algoId} description={`on ${serverId || "—"}`} />

      {!serverId && <div className="state error">Missing ?server= — open this page from the Algorithms list.</div>}

      {serverId && (
        <div className="grid cols-2">
          <div className="card">
            <h2>Overview</h2>
            <QueryBoundary query={algos}>
              {() =>
                !entry ? (
                  <div className="state error">Not registered.</div>
                ) : (
                  <dl className="kv">
                    <dt>Name</dt>
                    <dd>{entry.algo_id}</dd>
                    <dt>Server</dt>
                    <dd className="mono">{entry.server_id}</dd>
                    <dt>Mode</dt>
                    <dd style={{ textTransform: "uppercase" }}>{TRADING_MODE} · locked</dd>
                    <dt>Status</dt>
                    <dd>
                      <StatusBadge status={entry.status} />
                    </dd>
                    <dt>Enabled</dt>
                    <dd>{entry.enabled ? "yes" : "no"}</dd>
                    <dt>Script</dt>
                    <dd className="mono">{entry.script_path}</dd>
                    <dt>Last heartbeat</dt>
                    <dd>{relativeAge(entry.last_heartbeat)}</dd>
                  </dl>
                )
              }
            </QueryBoundary>
          </div>

          <div className="card">
            <h2>Performance</h2>
            <QueryBoundary query={pnl}>
              {(rows) =>
                rows.length === 0 ? (
                  <div className="state">No P&amp;L rows yet.</div>
                ) : (
                  <>
                    <div className="stat" style={{ marginBottom: 12 }}>
                      <span className="label">Latest daily P&amp;L ({todayPnl?.date})</span>
                      <span className={`value ${pnlSign(todayPnl?.pnl ?? null)}`}>{formatINR(todayPnl?.pnl ?? null)}</span>
                      <span className="sub">{todayPnl?.trade_count ?? 0} trades</span>
                    </div>
                    <div className="table-wrap">
                      <table className="data">
                        <thead>
                          <tr>
                            <th>Date</th>
                            <th className="num">P&amp;L</th>
                            <th className="num">Trades</th>
                          </tr>
                        </thead>
                        <tbody>
                          {rows.slice(0, 10).map((r) => (
                            <tr key={r.date}>
                              <td>{r.date}</td>
                              <td className={`num ${pnlSign(r.pnl)}`}>{formatINR(r.pnl)}</td>
                              <td className="num">{r.trade_count}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )
              }
            </QueryBoundary>
          </div>

          <div className="card">
            <h2>Open positions</h2>
            <QueryBoundary query={positions} empty={(d) => d.length === 0}>
              {(rows) => (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Symbol</th>
                        <th className="num">Qty</th>
                        <th className="num">Avg</th>
                        <th className="num">P&amp;L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((p) => (
                        <tr key={p.symbol}>
                          <td>{p.symbol}</td>
                          <td className="num">{p.quantity}</td>
                          <td className="num">{p.average_price}</td>
                          <td className={`num ${pnlSign(p.pnl)}`}>{p.pnl == null ? "—" : formatINR(p.pnl)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </QueryBoundary>
          </div>

          <div className="card">
            <h2>Recent trades</h2>
            <QueryBoundary query={trades} empty={(d) => d.length === 0}>
              {(rows) => (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th className="num">Qty</th>
                        <th className="num">Price</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((t, i) => (
                        <tr key={`${t.order_id ?? "x"}-${i}`}>
                          <td>{formatIST(t.executed_at)}</td>
                          <td>{t.symbol}</td>
                          <td className={t.side === "BUY" ? "pos" : "neg"}>{t.side}</td>
                          <td className="num">{t.quantity}</td>
                          <td className="num">{t.price}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </QueryBoundary>
          </div>

          <div className="card" style={{ gridColumn: "1 / -1" }}>
            <h2>Recent logs</h2>
            <QueryBoundary query={logs} empty={(d) => d.length === 0}>
              {(rows) => (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Level</th>
                        <th>Event</th>
                        <th>Details</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((l, i) => (
                        <tr key={i}>
                          <td>{formatIST(l.timestamp)}</td>
                          <td className={l.level === "ERROR" ? "neg" : l.level === "WARNING" ? "" : "zero"}>{l.level}</td>
                          <td>{l.event}</td>
                          <td style={{ whiteSpace: "normal", color: "var(--text-dim)" }}>
                            {l.details ? JSON.stringify(l.details) : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </QueryBoundary>
          </div>
        </div>
      )}
    </>
  );
}
