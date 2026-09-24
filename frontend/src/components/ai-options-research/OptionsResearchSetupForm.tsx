import { useState } from "react";
import { useSnapshotPreview } from "@/api/aiOptionsResearchHooks";
import { useSubmitOptionsResearch } from "@/api/aiOptionsResearchHooks";
import { isDisabled } from "@/api/aiOptionsResearchTypes";
import type { StrategyUniversePreset } from "@/api/aiOptionsResearchTypes";
import { QueryBoundary } from "@/components/States";

const UNDERLYINGS = ["NIFTY", "SENSEX"] as const;

const RISK_WARNING = (
  <div className="card" role="alert" style={{ marginBottom: 16, borderColor: "var(--warn, #b45309)" }}>
    <strong>AI Research Only</strong>
    <p className="sub" style={{ marginTop: 4 }}>
      This analysis is for research purposes. It does not place, modify, or cancel orders and is not
      connected to broker execution.
    </p>
  </div>
);

export function OptionsResearchSetupForm({ onSubmitted }: { onSubmitted: (researchId: string) => void }) {
  const [underlying, setUnderlying] = useState<string>("NIFTY");
  const [expiry, setExpiry] = useState<string>("nearest");
  const [useAsOf, setUseAsOf] = useState(false);
  const [asOf, setAsOf] = useState<string>("");
  const [preset, setPreset] = useState<StrategyUniversePreset>("DEFINED_RISK_ONLY");
  const [candidateLimit, setCandidateLimit] = useState(3);
  const [debateRounds, setDebateRounds] = useState(1);
  const [provider, setProvider] = useState("openai");
  const [quickModel, setQuickModel] = useState("gpt-4o-mini");
  const [deepModel, setDeepModel] = useState("");

  const preview = useSnapshotPreview(underlying, expiry);
  const submit = useSubmitOptionsResearch();

  async function handleSubmit() {
    const result = await submit.mutateAsync({
      underlying, expiry,
      as_of: useAsOf && asOf ? new Date(asOf).toISOString() : null,
      strategy_universe_preset: preset,
      candidate_limit: candidateLimit,
      debate_rounds: debateRounds,
      llm_provider: provider,
      quick_think_llm: quickModel,
      deep_think_llm: deepModel || null,
    });
    if (!isDisabled(result)) onSubmitted(result.research_id);
  }

  return (
    <>
      {RISK_WARNING}
      <div className="card" style={{ marginBottom: 16 }}>
        <h2>AI Options Research Setup</h2>
        <div className="form-row" style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <label>
            Underlying{" "}
            <select value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
              {UNDERLYINGS.map((u) => <option key={u} value={u}>{u}</option>)}
            </select>
          </label>
          <label>
            Expiry{" "}
            <select value={expiry} onChange={(e) => setExpiry(e.target.value)}>
              <option value="nearest">Nearest</option>
              <option value="next">Next</option>
            </select>
          </label>
          <label>
            <input type="checkbox" checked={useAsOf} onChange={(e) => setUseAsOf(e.target.checked)} /> Historical as-of
          </label>
          {useAsOf && <input type="datetime-local" value={asOf} onChange={(e) => setAsOf(e.target.value)} />}
        </div>

        <div className="form-row" style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 12 }}>
          <label>
            Strategy Universe{" "}
            <select value={preset} onChange={(e) => setPreset(e.target.value as StrategyUniversePreset)}>
              <option value="DEFINED_RISK_ONLY">Defined-risk only (default)</option>
              <option value="ALL_SUPPORTED">All supported</option>
            </select>
          </label>
          <label>
            Candidate Limit{" "}
            <input type="number" min={1} max={10} value={candidateLimit} onChange={(e) => setCandidateLimit(Number(e.target.value) || 3)} style={{ width: 60 }} />
          </label>
          <label>
            Debate Rounds{" "}
            <input type="number" min={1} max={5} value={debateRounds} onChange={(e) => setDebateRounds(Number(e.target.value) || 1)} style={{ width: 60 }} />
          </label>
        </div>

        <details style={{ marginTop: 12 }}>
          <summary>Advanced settings</summary>
          <div className="form-row" style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
            <label>Provider <input value={provider} onChange={(e) => setProvider(e.target.value)} /></label>
            <label>Quick model <input value={quickModel} onChange={(e) => setQuickModel(e.target.value)} /></label>
            <label>Deep model <input value={deepModel} onChange={(e) => setDeepModel(e.target.value)} placeholder="defaults to quick model" /></label>
          </div>
        </details>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Snapshot Preview</h2>
        <QueryBoundary query={preview}>
          {(data) =>
            isDisabled(data) ? (
              <div className="state">AI Research Engine is currently disabled.</div>
            ) : (
              <>
                <dl className="kv">
                  <dt>Spot</dt><dd>{data.spot ?? "—"}</dd>
                  <dt>Expiry</dt><dd>{data.expiry ?? "—"}</dd>
                  <dt>ATM Strike</dt><dd>{data.atm_strike ?? "—"}</dd>
                  <dt>DTE</dt><dd>{data.dte_calendar ?? "—"}</dd>
                  <dt>OI PCR</dt><dd>{data.oi_pcr ?? "—"}</dd>
                  <dt>Max Pain</dt><dd>{data.max_pain_strike ?? "—"}</dd>
                  <dt>ATM IV</dt><dd>{data.atm_iv ?? "—"}</dd>
                  <dt>Expected Move</dt><dd>±{data.expected_move ?? "—"}</dd>
                  <dt>OI Support</dt><dd>{data.oi_support.join(", ") || "—"}</dd>
                  <dt>OI Resistance</dt><dd>{data.oi_resistance.join(", ") || "—"}</dd>
                  <dt>Data Quality</dt><dd>{data.data_quality}</dd>
                  <dt>Evidence Quality</dt><dd>{data.evidence_quality} — {data.evidence_quality_reason}</dd>
                </dl>
                <button onClick={handleSubmit} disabled={submit.isPending} style={{ marginTop: 12 }}>
                  {submit.isPending ? "Starting…" : "Run AI Options Research"}
                </button>
              </>
            )
          }
        </QueryBoundary>
      </div>
    </>
  );
}
