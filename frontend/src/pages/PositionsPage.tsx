import { useState } from "react";
import { usePositions } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { AlgoPicker, type AlgoRef } from "@/components/AlgoPicker";
import { QueryBoundary } from "@/components/States";
import { formatINR, formatIST, formatNumber, pnlSign } from "@/lib/format";

export function PositionsPage() {
  const [ref, setRef] = useState<AlgoRef | null>(null);
  const positions = usePositions(ref?.algoId ?? null, ref?.serverId ?? null);

  return (
    <>
      <PageHeader
        title="Positions"
        description="Current open positions per strategy (GET /api/positions)."
        actions={
          <button className="sm" onClick={() => positions.refetch()} disabled={!ref}>
            Refresh
          </button>
        }
      />

      <div className="toolbar">
        <AlgoPicker value={ref} onChange={setRef} />
      </div>

      {!ref ? (
        <div className="state">Pick a strategy.</div>
      ) : (
        <QueryBoundary query={positions} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="num">Qty</th>
                    <th className="num">Avg price</th>
                    <th className="num">Last price</th>
                    <th className="num">P&L</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((p) => (
                    <tr key={p.symbol}>
                      <td>{p.symbol}</td>
                      <td className="num">{formatNumber(p.quantity)}</td>
                      <td className="num">{formatINR(p.average_price)}</td>
                      <td className="num">{p.last_price == null ? "—" : formatINR(p.last_price)}</td>
                      <td className={`num ${pnlSign(p.pnl)}`}>{p.pnl == null ? "—" : formatINR(p.pnl)}</td>
                      <td>{formatIST(p.updated_at)}</td>
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
