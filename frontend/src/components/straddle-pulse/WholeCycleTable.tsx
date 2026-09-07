import { useSessionOI } from "@/api/hooks";
import type { StraddleSession } from "@/api/types";
import { formatDateLabel } from "./format";

const LAKH = 100000;

function SessionRow({ session }: { session: StraddleSession }) {
  const oi = useSessionOI(session.id);
  const latest = oi.data?.points[oi.data.points.length - 1];
  return (
    <tr>
      <td>{formatDateLabel(session.trading_date)}</td>
      <td className="num">{session.atm_strike != null ? session.atm_strike.toLocaleString("en-IN") : "—"}</td>
      <td className="num">{latest ? `${latest.put_oi_change >= 0 ? "+" : ""}${(latest.put_oi_change / LAKH).toFixed(1)} L` : "—"}</td>
      <td className="num">{latest ? `${latest.call_oi_change >= 0 ? "+" : ""}${(latest.call_oi_change / LAKH).toFixed(1)} L` : "—"}</td>
      <td className="num">{latest?.pcr != null ? latest.pcr.toFixed(2) : "—"}</td>
      <td>{session.session_status}</td>
    </tr>
  );
}

/** Spec section 23/24: each daily session keeps its own ATM -- this table
 * never blends them into a fictional single weekly position. */
export function WholeCycleTable({ sessions }: { sessions: StraddleSession[] }) {
  return (
    <div className="card">
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Date</th>
              <th className="num">ATM</th>
              <th className="num">Put OI Δ</th>
              <th className="num">Call OI Δ</th>
              <th className="num">PCR</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <SessionRow key={s.id} session={s} />
            ))}
          </tbody>
        </table>
      </div>
      {sessions.length === 0 && <div className="state">No trading sessions in this cycle yet.</div>}
    </div>
  );
}
