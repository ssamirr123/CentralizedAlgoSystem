import { useState } from "react";
import { useOptionsExpiries, useOptionsIntelligence } from "@/api/optionsHooks";
import { QueryBoundary } from "@/components/States";
import type { ChainRowOut } from "@/api/optionsTypes";

const UNDERLYINGS = ["NIFTY", "SENSEX"] as const;

/** Phase 9 -- deterministic options intelligence only. No strategy
 * recommendation is ever rendered here (Section 46/47 boundary). */
export function OptionsIntelligencePanel() {
  const [underlying, setUnderlying] = useState<string>("NIFTY");
  const [expiry, setExpiry] = useState<string>("");
  const [strikeWindow, setStrikeWindow] = useState<number>(5);
  const [useAsOf, setUseAsOf] = useState(false);
  const [asOf, setAsOf] = useState<string>("");

  const expiries = useOptionsExpiries(underlying);
  const effectiveExpiry = expiry || expiries.data?.[0]?.expiry || "";
  const intelligence = useOptionsIntelligence(
    underlying, effectiveExpiry, strikeWindow, useAsOf && asOf ? new Date(asOf).toISOString() : undefined,
  );

  return (
    <>
      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Options Intelligence Setup</h2>
        <div className="form-row" style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <label>
            Underlying{" "}
            <select value={underlying} onChange={(e) => { setUnderlying(e.target.value); setExpiry(""); }}>
              {UNDERLYINGS.map((u) => (
                <option key={u} value={u}>{u}</option>
              ))}
            </select>
          </label>
          <label>
            Expiry{" "}
            <select value={effectiveExpiry} onChange={(e) => setExpiry(e.target.value)} disabled={!expiries.data?.length}>
              {(expiries.data ?? []).map((e) => (
                <option key={e.expiry} value={e.expiry}>
                  {e.expiry} {e.is_nearest ? "(nearest)" : ""} {e.is_monthly ? "(monthly)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            Strike Window ±{" "}
            <input
              type="number" min={1} max={20} value={strikeWindow}
              onChange={(e) => setStrikeWindow(Number(e.target.value) || 5)}
              style={{ width: 60 }}
            />
          </label>
          <label>
            <input type="checkbox" checked={useAsOf} onChange={(e) => setUseAsOf(e.target.checked)} /> Historical as-of
          </label>
          {useAsOf && (
            <input type="datetime-local" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
          )}
        </div>
      </div>

      <QueryBoundary query={intelligence}>
        {(data) => (
          <>
            <div className="card" style={{ marginBottom: 16 }}>
              <h2>{data.underlying} Options Intelligence</h2>
              <dl className="kv">
                <dt>Spot</dt><dd>{data.spot ?? "—"}</dd>
                <dt>ATM Strike</dt><dd>{data.atm_strike ?? "—"}</dd>
                <dt>Expiry</dt><dd>{data.expiry ?? "—"}</dd>
                <dt>DTE (calendar / trading)</dt>
                <dd>{data.expiry_info?.days_to_expiry_calendar ?? "—"} / {data.expiry_info?.days_to_expiry_trading ?? "—"}</dd>
                <dt>OI PCR</dt><dd>{data.oi_pcr ?? "—"}</dd>
                <dt>Volume PCR</dt><dd>{data.volume_pcr ?? "—"}</dd>
                <dt>Max Pain</dt><dd>{data.max_pain_strike ?? "—"}</dd>
                <dt>ATM IV</dt><dd>{data.atm_iv ?? "—"} {data.atm_iv !== null && `(${data.atm_iv_source})`}</dd>
                <dt>Expected Move</dt><dd>{data.expected_move ?? "—"} ({data.expected_move_method})</dd>
                <dt>IV Rank / Percentile</dt>
                <dd>
                  {data.iv_rank_status === "AVAILABLE"
                    ? `${data.iv_rank} / ${data.iv_percentile}`
                    : `NOT_AVAILABLE (${data.iv_rank_reason})`}
                </dd>
                <dt>OI Support (put-derived)</dt><dd>{data.oi_support.join(", ") || "—"}</dd>
                <dt>OI Resistance (call-derived)</dt><dd>{data.oi_resistance.join(", ") || "—"}</dd>
                <dt>Data Quality</dt><dd>{data.quality}{data.quality_reasons.length > 0 && ` — ${data.quality_reasons.join("; ")}`}</dd>
              </dl>
              <p className="sub" style={{ marginTop: 8 }}>
                Source: {data.provenance.option_chain_source} · Calculation v{data.provenance.calculation_version} ·
                Risk-free rate: {(data.provenance.risk_free_rate * 100).toFixed(2)}% · Provider calls: {data.provenance.provider_call_count}
              </p>
            </div>

            <ChainTable rows={data.chain.rows} atmStrike={data.atm_strike} />
          </>
        )}
      </QueryBoundary>
    </>
  );
}

function ChainTable({ rows, atmStrike }: { rows: ChainRowOut[]; atmStrike: number | null }) {
  return (
    <div className="card">
      <h2>Option Chain</h2>
      <div className="table-wrap" style={{ overflowX: "auto" }}>
        <table className="data">
          <thead>
            <tr>
              <th colSpan={6} style={{ textAlign: "center" }}>CALL</th>
              <th>STRIKE</th>
              <th colSpan={6} style={{ textAlign: "center" }}>PUT</th>
            </tr>
            <tr>
              <th>OI</th><th>ΔOI</th><th>Vol</th><th>IV</th><th>Delta</th><th>LTP</th>
              <th></th>
              <th>LTP</th><th>Delta</th><th>IV</th><th>Vol</th><th>ΔOI</th><th>OI</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.strike} style={row.strike === atmStrike ? { fontWeight: "bold", background: "var(--row-highlight, rgba(120,120,255,0.08))" } : undefined}>
                <td>{row.call?.open_interest ?? "—"}</td>
                <td>{row.call?.change_in_oi ?? "—"}</td>
                <td>{row.call?.volume ?? "—"}</td>
                <td>{row.call?.iv ?? "—"}</td>
                <td>{row.call?.delta ?? "—"}</td>
                <td>{row.call?.ltp ?? "—"}</td>
                <td style={{ textAlign: "center" }}>{row.strike}</td>
                <td>{row.put?.ltp ?? "—"}</td>
                <td>{row.put?.delta ?? "—"}</td>
                <td>{row.put?.iv ?? "—"}</td>
                <td>{row.put?.volume ?? "—"}</td>
                <td>{row.put?.change_in_oi ?? "—"}</td>
                <td>{row.put?.open_interest ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
