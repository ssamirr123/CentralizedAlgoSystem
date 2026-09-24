import { useEffect, useState } from "react";
import { useAiResearchConfig } from "@/api/aiResearchHooks";
import { isDisabled } from "@/api/aiResearchTypes";
import type { AnalystKey, Market, ResearchDepth } from "@/api/aiResearchTypes";
import { useEstimateBacktest, useSubmitBacktest } from "@/api/aiResearchBacktestHooks";
import type { BacktestFrequency, BacktestRequest } from "@/api/aiResearchBacktestTypes";
import { Loading } from "@/components/States";

const ANALYST_LABELS: Record<AnalystKey, string> = {
  market: "Technical",
  fundamentals: "Fundamentals",
  news: "News",
  social: "Sentiment",
};

function daysAgoIso(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

export function BacktestSetupForm({ onSubmitted }: { onSubmitted: (backtestId: string) => void }) {
  const config = useAiResearchConfig();
  const estimateMutation = useEstimateBacktest();
  const submit = useSubmitBacktest();

  const [market, setMarket] = useState<Market>("US");
  const [symbolsText, setSymbolsText] = useState("AAPL");
  const [startDate, setStartDate] = useState(daysAgoIso(60));
  const [endDate, setEndDate] = useState(daysAgoIso(1));
  const [frequency, setFrequency] = useState<BacktestFrequency>("weekly");
  const [analysts, setAnalysts] = useState<AnalystKey[]>(["market", "fundamentals", "news", "social"]);
  const [depth, setDepth] = useState<ResearchDepth>("standard");
  const [provider, setProvider] = useState("");

  const symbols = symbolsText.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);

  function buildRequest(): BacktestRequest {
    return {
      symbols,
      start_date: startDate,
      end_date: endDate,
      frequency,
      market,
      selected_analysts: analysts,
      research_depth: depth,
      llm_provider: provider || null,
    };
  }

  // Re-estimate whenever the shape of the request changes -- Section 24:
  // "Estimated research runs: N" before submission, and a clear reason
  // when the request exceeds the configured safety limit.
  useEffect(() => {
    if (symbols.length === 0 || !startDate || !endDate) return;
    estimateMutation.mutate(buildRequest());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbolsText, startDate, endDate, frequency, market]);

  if (config.isLoading) return <Loading label="Loading configuration…" />;
  if (config.isError || !config.data) return <div className="state error">Could not load AI Research configuration.</div>;

  if (!config.data.enabled) {
    return (
      <div className="state">
        AI Research Engine is currently disabled.
        <br />
        Enable <code>AI_RESEARCH_ENABLED</code> on the backend to use backtesting.
      </div>
    );
  }

  const opts = config.data;
  const estimate = estimateMutation.data && !isDisabled(estimateMutation.data) ? estimateMutation.data : null;

  function toggleAnalyst(key: AnalystKey) {
    setAnalysts((prev) => (prev.includes(key) ? prev.filter((a) => a !== key) : [...prev, key]));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submit.isPending || analysts.length === 0 || estimate?.exceeds_limit) return;
    const result = await submit.mutateAsync(buildRequest());
    if (isDisabled(result)) return;
    onSubmitted(result.backtest_id);
  }

  return (
    <form className="card" onSubmit={handleSubmit}>
      <h2>New Backtest</h2>
      <p className="sub">
        What decisions would TradingAgents have produced across these historical dates, and how would they have
        scored? This is decision research, not a trading-strategy simulation.
      </p>

      <div className="field">
        <label htmlFor="bt-market">Market</label>
        <select
          id="bt-market"
          value={market}
          onChange={(e) => {
            const next = e.target.value as Market;
            setMarket(next);
            setSymbolsText(next === "INDIA" ? "NIFTY" : "AAPL");
          }}
        >
          <option value="US">US / Global</option>
          <option value="INDIA">India — NSE</option>
        </select>
      </div>

      <div className="field">
        <label htmlFor="bt-symbols">Symbols (comma-separated)</label>
        <input
          id="bt-symbols" value={symbolsText} onChange={(e) => setSymbolsText(e.target.value)}
          placeholder={market === "INDIA" ? "NIFTY, RELIANCE" : "AAPL, MSFT"} required
        />
        {market === "INDIA" && <p className="sub">Trading holidays and weekends are skipped automatically (NSE calendar).</p>}
      </div>

      <div className="field">
        <label htmlFor="bt-start">Start date</label>
        <input id="bt-start" type="date" value={startDate} max={daysAgoIso(0)} onChange={(e) => setStartDate(e.target.value)} required />
      </div>
      <div className="field">
        <label htmlFor="bt-end">End date</label>
        <input id="bt-end" type="date" value={endDate} max={daysAgoIso(0)} onChange={(e) => setEndDate(e.target.value)} required />
      </div>

      <div className="field">
        <label htmlFor="bt-frequency">Frequency</label>
        <select id="bt-frequency" value={frequency} onChange={(e) => setFrequency(e.target.value as BacktestFrequency)}>
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
          <option value="monthly">Monthly</option>
        </select>
      </div>

      <fieldset className="field">
        <legend>Analysts</legend>
        {opts.analysts.map((key) => (
          <label key={key} className="chk" htmlFor={`bt-analyst-${key}`}>
            <input id={`bt-analyst-${key}`} type="checkbox" checked={analysts.includes(key)} onChange={() => toggleAnalyst(key)} />
            {ANALYST_LABELS[key] ?? key}
          </label>
        ))}
        {analysts.length === 0 && <p className="form-error">Select at least one analyst.</p>}
      </fieldset>

      <div className="field">
        <label htmlFor="bt-depth">Research depth</label>
        <select id="bt-depth" value={depth} onChange={(e) => setDepth(e.target.value as ResearchDepth)}>
          {opts.research_depths.map((d) => (
            <option key={d} value={d}>
              {d[0].toUpperCase() + d.slice(1)}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="bt-provider">LLM provider</label>
        <select id="bt-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="">Backend default</option>
          {opts.llm_providers.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      {estimate && (
        <div className={`state ${estimate.exceeds_limit ? "error" : ""}`} style={{ marginTop: 12 }}>
          <strong>Estimated research runs: {estimate.total_runs}</strong>
          {" "}({estimate.symbol_count} symbol{estimate.symbol_count === 1 ? "" : "s"} × {estimate.date_count} date
          {estimate.date_count === 1 ? "" : "s"})
          {estimate.exceeds_limit && (
            <div style={{ marginTop: 4 }}>
              This request cannot run: {estimate.limit_message}
            </div>
          )}
        </div>
      )}

      {submit.isError && <p className="form-error">Could not submit backtest. Please try again.</p>}

      <div style={{ marginTop: 16 }}>
        <button type="submit" disabled={submit.isPending || analysts.length === 0 || !!estimate?.exceeds_limit}>
          {submit.isPending ? "Submitting…" : "Run Backtest"}
        </button>
      </div>
    </form>
  );
}
