import { useState } from "react";
import { useAiResearchConfig, useSubmitResearch } from "@/api/aiResearchHooks";
import { isDisabled } from "@/api/aiResearchTypes";
import type { AnalystKey, Market, ResearchDepth, ResearchRequest } from "@/api/aiResearchTypes";
import { useCalendar, useInstrumentSearch } from "@/api/marketHooks";
import { Loading } from "@/components/States";

const ANALYST_LABELS: Record<AnalystKey, string> = {
  market: "Technical",
  fundamentals: "Fundamentals",
  news: "News",
  social: "Sentiment",
};

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

export function ResearchSetupForm({ onSubmitted }: { onSubmitted: (researchId: string) => void }) {
  const config = useAiResearchConfig();
  const submit = useSubmitResearch();

  const [market, setMarket] = useState<Market>("US");
  const [symbol, setSymbol] = useState("AAPL");
  const [instrumentQuery, setInstrumentQuery] = useState("");
  const [date, setDate] = useState(todayIso());
  const [analysts, setAnalysts] = useState<AnalystKey[]>(["market", "fundamentals", "news", "social"]);
  const [depth, setDepth] = useState<ResearchDepth>("standard");
  const [provider, setProvider] = useState("");
  const [quickModel, setQuickModel] = useState("");
  const [deepModel, setDeepModel] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [debateRounds, setDebateRounds] = useState<string>("");
  const [riskRounds, setRiskRounds] = useState<string>("");
  const [retryCount, setRetryCount] = useState<string>("");
  const [dataVendorCategory, setDataVendorCategory] = useState<string>("");
  const [dataVendor, setDataVendor] = useState<string>("");
  const [checkpointEnabled, setCheckpointEnabled] = useState(false);

  if (config.isLoading) return <Loading label="Loading configuration…" />;
  if (config.isError) return <div className="state error">Could not load AI Research configuration.</div>;
  if (!config.data) return null;

  if (!config.data.enabled) {
    return (
      <div className="state">
        AI Research Engine is currently disabled.
        <br />
        Enable <code>AI_RESEARCH_ENABLED</code> on the backend to use research functionality.
      </div>
    );
  }

  const opts = config.data;

  function toggleAnalyst(key: AnalystKey) {
    setAnalysts((prev) => (prev.includes(key) ? prev.filter((a) => a !== key) : [...prev, key]));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submit.isPending || analysts.length === 0) return;

    const body: ResearchRequest = {
      symbol: symbol.trim().toUpperCase(),
      research_date: date,
      market,
      research_depth: depth,
      selected_analysts: analysts,
      llm_provider: provider || null,
      quick_think_llm: quickModel || null,
      deep_think_llm: deepModel || null,
      debate_rounds: debateRounds ? Number(debateRounds) : null,
      risk_debate_rounds: riskRounds ? Number(riskRounds) : null,
      retry_count: retryCount ? Number(retryCount) : null,
      data_vendors: dataVendorCategory && dataVendor ? { [dataVendorCategory]: dataVendor } : null,
      checkpoint_enabled: checkpointEnabled,
    };

    const result = await submit.mutateAsync(body);
    if (isDisabled(result)) return;
    onSubmitted(result.research_id);
  }

  return (
    <form className="card" onSubmit={handleSubmit}>
      <h2>New Research</h2>

      <div className="field">
        <label htmlFor="ai-research-market">Market</label>
        <select
          id="ai-research-market"
          value={market}
          onChange={(e) => {
            const next = e.target.value as Market;
            setMarket(next);
            setSymbol(next === "INDIA" ? "NIFTY" : "AAPL");
            setInstrumentQuery("");
          }}
        >
          <option value="US">US / Global</option>
          <option value="INDIA">India — NSE</option>
        </select>
      </div>

      {market === "INDIA" && (
        <IndiaInstrumentPicker symbol={symbol} onSelect={setSymbol} query={instrumentQuery} onQueryChange={setInstrumentQuery} />
      )}

      <div className="field">
        <label htmlFor="ai-research-symbol">Symbol</label>
        <input
          id="ai-research-symbol"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="AAPL"
          required
          maxLength={32}
        />
      </div>

      <div className="field">
        <label htmlFor="ai-research-date">Research date</label>
        <input
          id="ai-research-date"
          type="date"
          value={date}
          max={todayIso()}
          onChange={(e) => setDate(e.target.value)}
          required
        />
      </div>

      {market === "INDIA" && <TradingDateBanner market={market} date={date} />}

      <div className="field">
        <label htmlFor="ai-research-currency">Currency</label>
        <input id="ai-research-currency" value={market === "INDIA" ? "INR" : "USD"} disabled readOnly />
      </div>

      <fieldset className="field">
        <legend>Analysts</legend>
        {opts.analysts.map((key) => (
          <label key={key} className="chk" htmlFor={`analyst-${key}`}>
            <input
              id={`analyst-${key}`}
              type="checkbox"
              checked={analysts.includes(key)}
              onChange={() => toggleAnalyst(key)}
            />
            {ANALYST_LABELS[key] ?? key}
          </label>
        ))}
        {analysts.length === 0 && <p className="form-error">Select at least one analyst.</p>}
      </fieldset>

      <div className="field">
        <label htmlFor="ai-research-depth">Research depth</label>
        <select id="ai-research-depth" value={depth} onChange={(e) => setDepth(e.target.value as ResearchDepth)}>
          {opts.research_depths.map((d) => (
            <option key={d} value={d}>
              {d[0].toUpperCase() + d.slice(1)}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="ai-research-provider">LLM provider</label>
        <select id="ai-research-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="">Backend default</option>
          {opts.llm_providers.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      <button
        type="button"
        className="sm ghost"
        onClick={() => setAdvancedOpen((v) => !v)}
        aria-expanded={advancedOpen}
      >
        Advanced Settings {advancedOpen ? "▲" : "▼"}
      </button>

      {advancedOpen && (
        <div style={{ marginTop: 8 }}>
          <p className="sub">
            Quick/deep model are free-text: TradingAgents accepts any model name string for the chosen provider — the
            backend does not enumerate a fixed model list.
          </p>
          <div className="field">
            <label htmlFor="ai-research-quick-model">Quick-thinking model</label>
            <input
              id="ai-research-quick-model"
              value={quickModel}
              onChange={(e) => setQuickModel(e.target.value)}
              placeholder="Backend default"
            />
          </div>
          <div className="field">
            <label htmlFor="ai-research-deep-model">Deep-thinking model</label>
            <input
              id="ai-research-deep-model"
              value={deepModel}
              onChange={(e) => setDeepModel(e.target.value)}
              placeholder="Backend default"
            />
          </div>
          <div className="field">
            <label htmlFor="ai-research-debate-rounds">
              Debate rounds ({opts.debate_rounds_range[0]}–{opts.debate_rounds_range[1]})
            </label>
            <input
              id="ai-research-debate-rounds"
              type="number"
              min={opts.debate_rounds_range[0]}
              max={opts.debate_rounds_range[1]}
              value={debateRounds}
              onChange={(e) => setDebateRounds(e.target.value)}
              placeholder={`Depth default`}
            />
          </div>
          <div className="field">
            <label htmlFor="ai-research-risk-rounds">
              Risk debate rounds ({opts.risk_debate_rounds_range[0]}–{opts.risk_debate_rounds_range[1]})
            </label>
            <input
              id="ai-research-risk-rounds"
              type="number"
              min={opts.risk_debate_rounds_range[0]}
              max={opts.risk_debate_rounds_range[1]}
              value={riskRounds}
              onChange={(e) => setRiskRounds(e.target.value)}
              placeholder="Depth default"
            />
          </div>
          <div className="field">
            <label htmlFor="ai-research-retry-count">
              Retry count ({opts.retry_count_range[0]}–{opts.retry_count_range[1]})
            </label>
            <input
              id="ai-research-retry-count"
              type="number"
              min={opts.retry_count_range[0]}
              max={opts.retry_count_range[1]}
              value={retryCount}
              onChange={(e) => setRetryCount(e.target.value)}
              placeholder="Provider default"
            />
          </div>
          <div className="field">
            <label htmlFor="ai-research-vendor-category">Data vendor override</label>
            <select
              id="ai-research-vendor-category"
              value={dataVendorCategory}
              onChange={(e) => {
                setDataVendorCategory(e.target.value);
                setDataVendor("");
              }}
            >
              <option value="">None</option>
              {Object.keys(opts.data_vendors).map((cat) => (
                <option key={cat} value={cat}>
                  {cat}
                </option>
              ))}
            </select>
            {dataVendorCategory && (
              <select value={dataVendor} onChange={(e) => setDataVendor(e.target.value)} style={{ marginTop: 4 }}>
                <option value="">Select vendor</option>
                {opts.data_vendors[dataVendorCategory]?.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            )}
          </div>
          <label className="chk" htmlFor="ai-research-checkpoint">
            <input
              id="ai-research-checkpoint"
              type="checkbox"
              checked={checkpointEnabled}
              onChange={(e) => setCheckpointEnabled(e.target.checked)}
            />
            Enable checkpointing (allows a later Resume attempt)
          </label>
        </div>
      )}

      {submit.isError && <p className="form-error">Could not submit research. Please try again.</p>}

      <div style={{ marginTop: 16 }}>
        <button type="submit" disabled={submit.isPending || analysts.length === 0}>
          {submit.isPending ? "Submitting…" : "Run AI Research"}
        </button>
      </div>
    </form>
  );
}

/** Section 26: backend-supported instrument search -- never a hardcoded
 * frontend stock list. */
function IndiaInstrumentPicker({
  symbol, onSelect, query, onQueryChange,
}: {
  symbol: string; onSelect: (symbol: string) => void; query: string; onQueryChange: (q: string) => void;
}) {
  const search = useInstrumentSearch("INDIA", query);
  const results = search.data?.items ?? [];

  return (
    <div className="field">
      <label htmlFor="ai-research-instrument-search">Instrument</label>
      <input
        id="ai-research-instrument-search"
        value={query}
        onChange={(e) => onQueryChange(e.target.value)}
        placeholder="Search NIFTY, BANKNIFTY, RELIANCE…"
      />
      {query.trim().length > 0 && results.length > 0 && (
        <ul className="search-results" style={{ listStyle: "none", padding: 0, margin: "4px 0" }}>
          {results.map((inst) => (
            <li key={inst.canonical_id}>
              <button
                type="button"
                className="sm ghost"
                onClick={() => {
                  onSelect(inst.symbol);
                  onQueryChange("");
                }}
              >
                {inst.display_name} ({inst.symbol}) — {inst.instrument_type}
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="sub">Selected: {symbol}</p>
    </div>
  );
}

/** Section 19: honest non-trading-date feedback -- never a silently
 * substituted date. */
function TradingDateBanner({ market, date }: { market: "INDIA"; date: string }) {
  const calendar = useCalendar(market, date);
  const v = calendar.data?.validation;
  if (!v || v.is_valid) return null;
  return (
    <div className="state error" role="alert">
      {v.reason}
      {v.previous_trading_day && v.next_trading_day && (
        <div className="sub">
          Previous trading day: {v.previous_trading_day} · Next trading day: {v.next_trading_day}
        </div>
      )}
    </div>
  );
}
