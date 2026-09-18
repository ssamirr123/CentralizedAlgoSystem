import { useExecutionPositions } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { formatINR, pnlSign } from "@/lib/format";

export function TccPositionsPage() {
  const positions = useExecutionPositions();

  return (
    <>
      <PageHeader
        title="Positions"
        description="Simulated/shadow positions reported by the execution framework. Never a real broker account's positions unless a future phase wires live execution behind an explicit safety gate."
      />
      <QueryBoundary query={positions}>
        {(list) =>
          list.length === 0 ? (
            <div className="state">
              No positions yet — no strategy currently generates order intents (Phase 10's deliberate scope).
            </div>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Account</th>
                    <th>Symbol</th>
                    <th className="num">Qty</th>
                    <th className="num">Avg price</th>
                    <th className="num">Last price</th>
                    <th className="num">P&amp;L</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((p, i) => (
                    <tr key={`${p.strategy_id}|${p.account_id}|${p.symbol}|${i}`}>
                      <td>{p.strategy_id}</td>
                      <td className="mono">{p.account_id}</td>
                      <td>{p.symbol}</td>
                      <td className="num">{p.quantity}</td>
                      <td className="num">{formatINR(p.average_price)}</td>
                      <td className="num">{formatINR(p.last_price)}</td>
                      <td className={`num ${pnlSign(p.pnl)}`}>{formatINR(p.pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </QueryBoundary>
    </>
  );
}
