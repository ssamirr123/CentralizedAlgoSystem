import type { CandidateOut } from "@/api/aiOptionsResearchTypes";

function fmt(v: number | null | undefined, unbounded?: boolean): string {
  if (unbounded) return "UNBOUNDED";
  if (v === null || v === undefined) return "—";
  return String(v);
}

export function CandidateCard({ candidate, index }: { candidate: CandidateOut; index: number }) {
  if (!candidate.supported) {
    return (
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Candidate {index + 1}: {candidate.strategy.replaceAll("_", " ")}</h3>
        <p className="state">Unsupported: {candidate.unsupported_reason}</p>
      </div>
    );
  }
  const payoff = candidate.payoff;
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3>Candidate {index + 1}: {candidate.strategy.replaceAll("_", " ")}</h3>

      <h4 style={{ marginBottom: 4 }}>Deterministic Metrics</h4>
      <div className="table-wrap" style={{ overflowX: "auto", marginBottom: 8 }}>
        <table className="data">
          <thead>
            <tr><th>Side</th><th>Type</th><th>Strike</th><th>Price</th><th>Source</th><th>Liquidity</th></tr>
          </thead>
          <tbody>
            {candidate.legs.map((leg, i) => (
              <tr key={i}>
                <td>{leg.side}</td><td>{leg.option_type}</td><td>{leg.strike}</td>
                <td>{leg.price ?? "—"}</td><td>{leg.price_source}</td>
                <td>{leg.liquidity_ok ? "OK" : `FLAGGED: ${leg.liquidity_reasons.join("; ")}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {payoff && (
        <dl className="kv">
          <dt>Net Credit/Debit</dt><dd>{payoff.net_premium ?? "—"}</dd>
          <dt>Max Profit</dt><dd>{fmt(payoff.max_profit, payoff.max_profit_unbounded)}</dd>
          <dt>Max Loss</dt><dd>{fmt(payoff.max_loss, payoff.max_loss_unbounded)}</dd>
          <dt>Breakevens</dt><dd>{payoff.breakevens.join(", ") || "—"}</dd>
          <dt>Pricing Method</dt><dd className="sub">{payoff.pricing_method}</dd>
        </dl>
      )}
      {!candidate.liquidity_ok && <p className="state error">Liquidity gate flagged one or more legs (see table above).</p>}

      {candidate.commentary && (
        <>
          <h4 style={{ marginTop: 12, marginBottom: 4 }}>AI Interpretation</h4>
          <p><strong>Why it fits:</strong> {candidate.commentary.fits_reasoning}</p>
          <p><strong>Invalidation:</strong> {candidate.commentary.invalidation}</p>
          <p><strong>Market assumptions:</strong> {candidate.commentary.market_assumptions}</p>
          <p><strong>Volatility assumptions:</strong> {candidate.commentary.volatility_assumptions}</p>
        </>
      )}
      {candidate.risk_analysis && (
        <>
          <h4 style={{ marginTop: 12, marginBottom: 4 }}>Risk Analysis (AI Interpretation)</h4>
          <ul>
            <li><strong>Directional:</strong> {candidate.risk_analysis.directional_exposure}</li>
            <li><strong>Volatility:</strong> {candidate.risk_analysis.volatility_exposure}</li>
            <li><strong>Theta:</strong> {candidate.risk_analysis.theta_exposure}</li>
            <li><strong>Gamma:</strong> {candidate.risk_analysis.gamma_risk}</li>
            <li><strong>Gap:</strong> {candidate.risk_analysis.gap_risk}</li>
            <li><strong>Expiry:</strong> {candidate.risk_analysis.expiry_risk}</li>
            <li><strong>Liquidity:</strong> {candidate.risk_analysis.liquidity_risk}</li>
            <li><strong>Assignment:</strong> {candidate.risk_analysis.assignment_considerations}</li>
            <li><strong>Event:</strong> {candidate.risk_analysis.event_risk}</li>
            <li><strong>Tail:</strong> {candidate.risk_analysis.tail_risk}</li>
          </ul>
        </>
      )}
    </div>
  );
}

export function CandidateComparisonTable({ candidates }: { candidates: CandidateOut[] }) {
  const supported = candidates.filter((c) => c.supported);
  if (supported.length < 2) return null;
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3>Candidate Comparison</h3>
      <div className="table-wrap" style={{ overflowX: "auto" }}>
        <table className="data">
          <thead>
            <tr>
              <th>Metric</th>
              {supported.map((c) => <th key={c.strategy}>{c.strategy.replaceAll("_", " ")}</th>)}
            </tr>
          </thead>
          <tbody>
            <tr><td>Credit/Debit</td>{supported.map((c) => <td key={c.strategy}>{c.payoff?.net_premium ?? "—"}</td>)}</tr>
            <tr><td>Max Profit</td>{supported.map((c) => <td key={c.strategy}>{fmt(c.payoff?.max_profit, c.payoff?.max_profit_unbounded)}</td>)}</tr>
            <tr><td>Max Loss</td>{supported.map((c) => <td key={c.strategy}>{fmt(c.payoff?.max_loss, c.payoff?.max_loss_unbounded)}</td>)}</tr>
            <tr><td>Breakevens</td>{supported.map((c) => <td key={c.strategy}>{c.payoff?.breakevens.join(", ") || "—"}</td>)}</tr>
            <tr><td>Liquidity</td>{supported.map((c) => <td key={c.strategy}>{c.liquidity_ok ? "OK" : "Flagged"}</td>)}</tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
