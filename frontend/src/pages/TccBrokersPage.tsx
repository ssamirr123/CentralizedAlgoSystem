import { useExecutionBrokers } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { brokerLabel } from "@/lib/format";

export function TccBrokersPage() {
  const brokers = useExecutionBrokers();

  return (
    <>
      <PageHeader
        title="Brokers"
        description="Broker-type availability (Phase 7) — independent of any single account's own enabled flag. An unavailable broker's accounts cannot be assigned to a strategy."
      />
      <QueryBoundary query={brokers} empty={(d) => d.length === 0}>
        {(list) => (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Broker</th>
                  <th>Available</th>
                  <th>Reason</th>
                  <th className="num">Accounts</th>
                </tr>
              </thead>
              <tbody>
                {list.map((b) => (
                  <tr key={b.broker_id}>
                    <td>{brokerLabel(b.broker_id)}</td>
                    <td>
                      {b.available ? (
                        <span className="badge running">
                          <span className="dot" />
                          AVAILABLE
                        </span>
                      ) : (
                        <span className="badge error">
                          <span className="dot" />
                          UNAVAILABLE
                        </span>
                      )}
                    </td>
                    <td style={{ color: "var(--text-dim)" }}>{b.reason || "—"}</td>
                    <td className="num">{b.account_count}</td>
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
