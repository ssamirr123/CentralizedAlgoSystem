import { useMemo, useState } from "react";
import { usePnlToday, usePnlHistory } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { AlgoPicker, type AlgoRef } from "@/components/AlgoPicker";
import { QueryBoundary } from "@/components/States";
import { formatINR, pnlSign } from "@/lib/format";

function istToday(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
}

export function PnlPage() {
  const [pnlDate, setPnlDate] = useState(istToday());
  const [ref, setRef] = useState<AlgoRef | null>(null);
  const today = usePnlToday(pnlDate);
  const history = usePnlHistory(ref?.algoId ?? null, ref?.serverId ?? null);

  const rows = useMemo(() => Object.entries(today.data ?? {}).sort(([a], [b]) => a.localeCompare(b)), [today.data]);
  const total = rows.reduce((s, [, v]) => s + v, 0);

  return (
    <>
      <PageHeader title="P&L" description="Daily P&L rollups (GET /api/pnl/today and GET /api/pnl)." />

      <div className="toolbar">
        <div className="field">
          <label htmlFor="pnlDate">P&L date</label>
          <input id="pnlDate" type="date" value={pnlDate} onChange={(e) => setPnlDate(e.target.value)} />
        </div>
        <button className="sm" onClick={() => today.refetch()}>
          Refresh
        </button>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>All strategies — {pnlDate}</h2>
        <QueryBoundary query={today} empty={(d) => Object.keys(d).length === 0}>
          {() => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy | Server</th>
                    <th className="num">Day P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(([k, v]) => (
                    <tr key={k}>
                      <td className="mono">{k}</td>
                      <td className={`num ${pnlSign(v)}`}>{formatINR(v)}</td>
                    </tr>
                  ))}
                  <tr>
                    <td style={{ fontWeight: 700 }}>Total</td>
                    <td className={`num ${pnlSign(total)}`} style={{ fontWeight: 700 }}>
                      {formatINR(total)}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>
      </div>

      <div className="card">
        <h2>History for one strategy</h2>
        <div className="toolbar">
          <AlgoPicker value={ref} onChange={setRef} />
        </div>
        {!ref ? (
          <div className="state">Pick a strategy.</div>
        ) : (
          <QueryBoundary query={history} empty={(d) => d.length === 0}>
            {(list) => (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th className="num">P&L</th>
                      <th className="num">Trades</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((r) => (
                      <tr key={r.date}>
                        <td>{r.date}</td>
                        <td className={`num ${pnlSign(r.pnl)}`}>{formatINR(r.pnl)}</td>
                        <td className="num">{r.trade_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </QueryBoundary>
        )}
      </div>
    </>
  );
}
