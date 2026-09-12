import { useExecutionAccounts } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { brokerLabel } from "@/lib/format";

export function TccAccountsPage() {
  const accounts = useExecutionAccounts();

  return (
    <>
      <PageHeader
        title="Trading Accounts"
        description="TradingAccount registry (Phase 1/7) — read-only here. Credentials are never exposed by the backend and are not shown in this UI."
      />
      <QueryBoundary query={accounts} empty={(d) => d.length === 0}>
        {(list) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Account</th>
                  <th>Name</th>
                  <th>Broker</th>
                  <th>Enabled</th>
                  <th>Connection</th>
                  <th>Environment</th>
                  <th>Execution mode</th>
                </tr>
              </thead>
              <tbody>
                {list.map((a) => (
                  <tr key={a.account_id}>
                    <td className="mono">{a.account_id}</td>
                    <td>{a.account_name}</td>
                    <td>{brokerLabel(a.broker_id)}</td>
                    <td>{a.enabled ? "yes" : "no"}</td>
                    <td>
                      <StatusBadge status={a.connection_state} />
                    </td>
                    <td>{a.environment}</td>
                    <td>{a.execution_mode}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryBoundary>
    </>
  );
}
