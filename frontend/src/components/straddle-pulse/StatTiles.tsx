import type { StraddleCycle, StraddleSession, StraddleSessionChart, StraddleSessionOI } from "@/api/types";
import { formatDateLabel } from "./format";
import { buildStraddleSeries } from "./straddleSeries";

const LAKH = 100000;

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="stat-tile">
      <div className="stat-tile-label">{label}</div>
      <div className="stat-tile-value">{value}</div>
      {sub && <div className="stat-tile-sub">{sub}</div>}
    </div>
  );
}

export function StatTiles({
  session, cycle, chart, oi,
}: {
  session: StraddleSession;
  cycle: StraddleCycle;
  chart: StraddleSessionChart | undefined;
  oi: StraddleSessionOI | undefined;
}) {
  const points = chart ? buildStraddleSeries(chart.spot, chart.atm_ce, chart.atm_pe) : [];
  const open = points[0]?.v ?? null;
  const now = points[points.length - 1]?.v ?? null;
  const delta = open != null && now != null ? now - open : null;
  const deltaPct = open ? ((delta ?? 0) / open) * 100 : null;

  const latestOI = oi?.points[oi.points.length - 1];
  const expiry = new Date(cycle.expiry_date);
  const asOf = new Date(session.trading_date);
  const dte = Math.max(0, Math.round((expiry.getTime() - asOf.getTime()) / 86400000));

  return (
    <div className="stat-tiles">
      <Tile
        label="09:16 Spot"
        value={session.spot_0916 != null ? session.spot_0916.toLocaleString("en-IN") : "—"}
        sub={short(session.trading_date)}
      />
      <Tile
        label={`ATM (${short(session.trading_date)})`}
        value={session.atm_strike != null ? session.atm_strike.toLocaleString("en-IN") : "—"}
        sub="daily 09:16"
      />
      <div className="stat-tile">
        <div className="stat-tile-label">ATM CE / PE</div>
        <div className="stat-tile-ce-pe">
          <div>
            <div className="stat-tile-value">{session.atm_strike != null ? `${session.atm_strike.toLocaleString("en-IN")} CE` : "—"}</div>
            <div className="stat-tile-sub">{points[0] ? `open ${points[0].ce.toFixed(1)}` : session.atm_ce_symbol ?? " "}</div>
          </div>
          <div>
            <div className="stat-tile-value">{session.atm_strike != null ? `${session.atm_strike.toLocaleString("en-IN")} PE` : "—"}</div>
            <div className="stat-tile-sub">{points[0] ? `open ${points[0].pe.toFixed(1)}` : session.atm_pe_symbol ?? " "}</div>
          </div>
        </div>
      </div>
      <Tile label="Expiry" value={cycle.expiry_date} sub={`${dte} DTE`} />
      <Tile
        label="Straddle now"
        value={now != null ? `₹${now.toFixed(1)}` : "—"}
        sub={open != null ? `open ${open.toFixed(1)}` : undefined}
      />
      <Tile
        label="Δ intraday"
        value={delta != null ? `${delta >= 0 ? "▲" : "▼"} ₹${Math.abs(delta).toFixed(1)}` : "—"}
        sub={deltaPct != null ? `${deltaPct.toFixed(1)}%` : undefined}
      />
      <Tile
        label="Put OI now"
        value={latestOI ? `${(latestOI.put_oi_total / LAKH).toFixed(1)}L` : "—"}
        sub={latestOI ? `intra ${latestOI.put_oi_change >= 0 ? "▲+" : "▼"}${Math.abs(latestOI.put_oi_change / LAKH).toFixed(1)}L` : undefined}
      />
      <Tile
        label="Call OI now"
        value={latestOI ? `${(latestOI.call_oi_total / LAKH).toFixed(1)}L` : "—"}
        sub={latestOI ? `intra ${latestOI.call_oi_change >= 0 ? "▲+" : "▼"}${Math.abs(latestOI.call_oi_change / LAKH).toFixed(1)}L` : undefined}
      />
      <Tile
        label="PCR (Put/Call)"
        value={latestOI?.pcr != null ? latestOI.pcr.toFixed(2) : "—"}
        sub={latestOI?.pcr != null ? (latestOI.pcr < 0.7 ? "call-heavy" : latestOI.pcr > 1.3 ? "put-heavy" : "balanced") : undefined}
      />
    </div>
  );
}

function short(isoDate: string): string {
  return formatDateLabel(isoDate, { withMonth: false }).toUpperCase();
}
