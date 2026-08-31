import { useMemo, useState } from "react";
import { useLogs } from "@/api/hooks";
import type { LogQuery } from "@/api/endpoints";
import { PageHeader } from "@/components/PageHeader";
import { AlgoPicker, type AlgoRef } from "@/components/AlgoPicker";
import { QueryBoundary } from "@/components/States";
import { formatIST } from "@/lib/format";

const LEVELS = ["", "INFO", "WARNING", "ERROR"];

export function LogsPage() {
  const [ref, setRef] = useState<AlgoRef | null>(null);
  const [level, setLevel] = useState("");
  const [event, setEvent] = useState("");
  const [logDate, setLogDate] = useState("");
  const [limit, setLimit] = useState(100);

  const query: LogQuery | null = useMemo(
    () =>
      ref
        ? {
            algo_id: ref.algoId,
            server_id: ref.serverId,
            limit,
            level: level || undefined,
            event: event || undefined,
            log_date: logDate || undefined,
          }
        : null,
    [ref, level, event, logDate, limit],
  );

  const logs = useLogs(query);

  return (
    <>
      <PageHeader title="Logs" description="Shipped strategy log events (GET /api/logs)." />

      <div className="toolbar">
        <AlgoPicker value={ref} onChange={setRef} />
        <div className="field" style={{ minWidth: 130 }}>
          <label htmlFor="level">Level</label>
          <select id="level" value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l || "any"}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ minWidth: 160 }}>
          <label htmlFor="event">Event</label>
          <input id="event" value={event} onChange={(e) => setEvent(e.target.value)} placeholder="e.g. ALGO_STARTED" />
        </div>
        <div className="field" style={{ minWidth: 150 }}>
          <label htmlFor="logDate">Date (UTC)</label>
          <input id="logDate" type="date" value={logDate} onChange={(e) => setLogDate(e.target.value)} />
        </div>
        <div className="field" style={{ minWidth: 100 }}>
          <label htmlFor="limit">Limit</label>
          <select id="limit" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[50, 100, 200, 500].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
        <button className="sm" onClick={() => logs.refetch()} disabled={!ref}>
          Refresh
        </button>
      </div>

      {!ref ? (
        <div className="state">Pick a strategy.</div>
      ) : (
        <QueryBoundary query={logs} empty={(d) => d.length === 0}>
          {(list) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Time (IST)</th>
                    <th>Level</th>
                    <th>Event</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((l, i) => (
                    <tr key={`${l.timestamp}-${i}`}>
                      <td>{formatIST(l.timestamp)}</td>
                      <td className={l.level === "ERROR" ? "neg" : l.level === "WARNING" ? "" : ""}>{l.level}</td>
                      <td>{l.event}</td>
                      <td style={{ whiteSpace: "normal" }}>
                        <code>{l.details ? JSON.stringify(l.details) : "—"}</code>
                      </td>
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
