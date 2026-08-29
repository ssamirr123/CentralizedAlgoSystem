import { useState } from "react";
import { useTrades } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { AlgoPicker, type AlgoRef } from "@/components/AlgoPicker";
import { QueryBoundary } from "@/components/States";
import { formatINR, formatIST, formatNumber } from "@/lib/format";

export function TradesPage() {
  const [ref, setRef] = useState<AlgoRef | null>(null);
  const [limit, setLimit] = useState(100);
  const trades = useTrades(ref?.algoId ?? null, ref?.serverId ?? null, limit);

  return (
    <>
      <PageHeader
        title="Trades"
        description="Executed fills, newest first (GET /api/trades). Insert-only history."
        actions={
          <button className="sm" onClick={() => trades.refetch()} disabled={!ref}>
            Refresh
          </button>
        }
      />

      <div className="toolbar">
        <AlgoPicker value={ref} onChange={setRef} />
        <div className="field" style={{ minWidth: 120 }}>
          <label htmlFor="limit">Limit</label>
          <select id="limit" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[50, 100, 200, 500].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
      </div>

      {!ref ? (
        <div className="state">Pick a strategy.</div>
      ) : (
        <QueryBoundary query={trades} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Executed (IST)</th>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th className="num">Qty</th>
                    <th className="num">Price</th>
                    <th>Order ID</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((t, i) => (
                    <tr key={t.order_id ?? `${t.executed_at}-${i}`}>
                      <td>{formatIST(t.executed_at)}</td>
                      <td>{t.symbol}</td>
                      <td className={t.side.toUpperCase() === "BUY" ? "pos" : "neg"}>{t.side.toUpperCase()}</td>
                      <td className="num">{formatNumber(t.quantity)}</td>
                      <td className="num">{formatINR(t.price)}</td>
                      <td className="mono">{t.order_id ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>
      )}
    </>
  );
}
