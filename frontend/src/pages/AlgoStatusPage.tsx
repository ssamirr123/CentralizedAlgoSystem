import { useState } from "react";
import { useAlgoStatus } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { AlgoPicker, type AlgoRef } from "@/components/AlgoPicker";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { formatIST } from "@/lib/format";

export function AlgoStatusPage() {
  const [ref, setRef] = useState<AlgoRef | null>(null);
  const status = useAlgoStatus(ref?.algoId ?? null, ref?.serverId ?? null);

  return (
    <>
      <PageHeader
        title="Algo Status"
        description="Live process status straight from the agent on the box (GET /api/algo/status → Lambda → SSM)."
        actions={
          <button className="sm" onClick={() => status.refetch()} disabled={!ref}>
            Re-check
          </button>
        }
      />

      <div className="toolbar">
        <AlgoPicker value={ref} onChange={setRef} />
      </div>

      {!ref ? (
        <div className="state">Pick a strategy to probe.</div>
      ) : (
        <div className="card">
          <QueryBoundary query={status}>
            {(d) => (
              <dl className="kv">
                <dt>Strategy</dt>
                <dd>{d.algo_id}</dd>
                <dt>Server</dt>
                <dd className="mono">{ref.serverId}</dd>
                <dt>Status</dt>
                <dd>
                  <StatusBadge status={d.status} />
                </dd>
                <dt>PID</dt>
                <dd className="mono">{d.pid ?? "—"}</dd>
                <dt>Started at</dt>
                <dd>{formatIST(d.started_at)}</dd>
                <dt>Probe succeeded</dt>
                <dd>{String(d.success)}</dd>
                {d.message && (
                  <>
                    <dt>Message</dt>
                    <dd>{d.message}</dd>
                  </>
                )}
              </dl>
            )}
          </QueryBoundary>
        </div>
      )}
    </>
  );
}
