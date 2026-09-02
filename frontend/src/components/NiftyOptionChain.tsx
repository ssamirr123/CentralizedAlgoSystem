import { useMemo, useState } from "react";
import { useNiftyExpiries, useNiftyOptionChain } from "@/api/hooks";
import { QueryBoundary } from "@/components/States";
import type { MarketOptionQuote } from "@/api/types";

const RANGES = [5, 10, 20];

function n(v: number | null | undefined, d = 2): string {
  return v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: d });
}

function Cells({ q }: { q: MarketOptionQuote | null }) {
  return (
    <>
      <td className="num">{n(q?.oi ?? null, 0)}</td>
      <td className="num">{n(q?.oi_change ?? null, 0)}</td>
      <td className="num">{n(q?.volume ?? null, 0)}</td>
      <td className="num">{n(q?.iv ?? null)}</td>
      <td className="num">{n(q?.ltp ?? null)}</td>
    </>
  );
}

export function NiftyOptionChain() {
  const expiries = useNiftyExpiries();
  const [expiry, setExpiry] = useState("current");
  const [range, setRange] = useState(10);
  const chain = useNiftyOptionChain(expiry, range);

  const expiryOptions = useMemo(
    () => ["current", "next", ...(expiries.data ?? [])],
    [expiries.data],
  );

  return (
    <div className="card">
      <div className="toolbar">
        <div className="field">
          <label>Expiry</label>
          <select value={expiry} onChange={(e) => setExpiry(e.target.value)}>
            {expiryOptions.map((e) => (
              <option key={e} value={e}>
                {e}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Strike range</label>
          <select value={range} onChange={(e) => setRange(Number(e.target.value))}>
            {RANGES.map((r) => (
              <option key={r} value={r}>
                ATM ± {r}
              </option>
            ))}
          </select>
        </div>
        <button className="sm" onClick={() => chain.refetch()}>
          Refresh
        </button>
        {chain.data && (
          <span className="conn" style={{ marginLeft: "auto" }}>
            Spot {n(chain.data.spot)} · ATM {n(chain.data.atm_strike, 0)} · {chain.data.expiry}
          </span>
        )}
      </div>

      <QueryBoundary query={chain} empty={(d) => d.strikes.length === 0}>
        {(d) => (
          <div className="table-wrap">
            <table className="data option-chain">
              <thead>
                <tr>
                  <th colSpan={5} style={{ textAlign: "center" }}>CALLS</th>
                  <th>STRIKE</th>
                  <th colSpan={5} style={{ textAlign: "center" }}>PUTS</th>
                </tr>
                <tr>
                  <th className="num">OI</th>
                  <th className="num">OI Chg</th>
                  <th className="num">Vol</th>
                  <th className="num">IV</th>
                  <th className="num">LTP</th>
                  <th></th>
                  <th className="num">LTP</th>
                  <th className="num">IV</th>
                  <th className="num">Vol</th>
                  <th className="num">OI Chg</th>
                  <th className="num">OI</th>
                </tr>
              </thead>
              <tbody>
                {d.strikes.map((r) => {
                  const atm = d.atm_strike != null && Math.abs(r.strike - d.atm_strike) < 1e-6;
                  return (
                    <tr key={r.strike} className={atm ? "atm-row" : ""}>
                      <Cells q={r.call} />
                      <td className="num" style={{ fontWeight: 700 }}>{n(r.strike, 0)}</td>
                      {/* puts, mirrored order */}
                      <td className="num">{n(r.put?.ltp ?? null)}</td>
                      <td className="num">{n(r.put?.iv ?? null)}</td>
                      <td className="num">{n(r.put?.volume ?? null, 0)}</td>
                      <td className="num">{n(r.put?.oi_change ?? null, 0)}</td>
                      <td className="num">{n(r.put?.oi ?? null, 0)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
