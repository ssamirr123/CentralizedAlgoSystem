import { useExecutionOrders } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";

export function TccOrdersPage() {
  const orders = useExecutionOrders();

  return (
    <>
      <PageHeader
        title="Orders"
        description="Orders generated through the execution framework's RiskManager → ExecutionEngine pipeline. This UI never places, modifies, or cancels an order itself — it only displays outcomes reported by the backend."
      />
      <QueryBoundary query={orders}>
        {(list) =>
          list.length === 0 ? (
            <div className="state">
              No orders yet. No strategy currently generates order intents (Phase 10's deliberate scope) — this page
              will populate once a future phase wires real decision logic through the execution pipeline.
            </div>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Order ID</th>
                    <th>Strategy</th>
                    <th>Account</th>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th className="num">Qty</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((o) => (
                    <tr key={o.order_id}>
                      <td className="mono">{o.order_id}</td>
                      <td>{o.strategy_id}</td>
                      <td className="mono">{o.account_id}</td>
                      <td>{o.symbol}</td>
                      <td>{o.side}</td>
                      <td className="num">{o.quantity}</td>
                      <td>
                        <StatusBadge status={o.status} />
                      </td>
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
