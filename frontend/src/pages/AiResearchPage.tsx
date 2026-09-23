import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDecisionMemory, useResearchHistory } from "@/api/aiResearchHooks";
import { isDisabled } from "@/api/aiResearchTypes";
import type { ResearchJobStatus } from "@/api/aiResearchTypes";
import { PageHeader } from "@/components/PageHeader";
import { QueryBoundary } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { ResearchSetupForm } from "@/components/ai-research/ResearchSetupForm";
import { BacktestSetupForm } from "@/components/ai-research/BacktestSetupForm";
import { OptionsIntelligencePanel } from "@/components/ai-research/OptionsIntelligencePanel";
import { OptionsResearchSetupForm } from "@/components/ai-options-research/OptionsResearchSetupForm";
import { useBacktestHistory } from "@/api/aiResearchBacktestHooks";
import { useOptionsResearchHistory } from "@/api/aiOptionsResearchHooks";
import { isDisabled as isOptionsResearchDisabled } from "@/api/aiOptionsResearchTypes";

type Tab = "new" | "history" | "memory" | "backtest" | "options" | "options_research";

export function AiResearchPage() {
  const [tab, setTab] = useState<Tab>("new");
  const navigate = useNavigate();

  return (
    <>
      <PageHeader title="AI Research Engine" description="TradingAgents-powered multi-agent market research" />

      <div className="session-tabs" role="tablist" style={{ marginBottom: 16 }}>
        {([
          ["new", "New Research"],
          ["history", "Research History"],
          ["backtest", "Backtesting"],
          ["options", "Options Intelligence"],
          ["options_research", "AI Options Research"],
          ["memory", "Decision Memory"],
        ] as [Tab, string][]).map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={`sm ${tab === key ? "active" : ""}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "new" && <ResearchSetupForm onSubmitted={(id) => navigate(`/ai-research/${id}`)} />}
      {tab === "history" && <HistoryTab onOpen={(id) => navigate(`/ai-research/${id}`)} />}
      {tab === "memory" && <MemoryTab />}
      {tab === "backtest" && <BacktestTab onSubmitted={(id) => navigate(`/ai-research/backtests/${id}`)} />}
      {tab === "options" && <OptionsIntelligencePanel />}
      {tab === "options_research" && (
        <OptionsResearchTab onSubmitted={(id) => navigate(`/ai-options-research/${id}`)} onOpen={(id) => navigate(`/ai-options-research/${id}`)} />
      )}
    </>
  );
}

function BacktestTab({ onSubmitted }: { onSubmitted: (backtestId: string) => void }) {
  const [subTab, setSubTab] = useState<"new" | "history">("new");
  const history = useBacktestHistory();

  return (
    <>
      <div className="session-tabs" role="tablist" style={{ marginBottom: 16 }}>
        <button role="tab" aria-selected={subTab === "new"} className={`sm ${subTab === "new" ? "active" : ""}`} onClick={() => setSubTab("new")}>
          New Backtest
        </button>
        <button role="tab" aria-selected={subTab === "history"} className={`sm ${subTab === "history" ? "active" : ""}`} onClick={() => setSubTab("history")}>
          Backtest History
        </button>
      </div>

      {subTab === "new" && <BacktestSetupForm onSubmitted={onSubmitted} />}
      {subTab === "history" && (
        <div className="card">
          <h2>Backtest History</h2>
          <QueryBoundary query={history} empty={(d) => isDisabled(d) || d.items.length === 0}>
            {(data) =>
              isDisabled(data) ? null : (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Symbols</th>
                        <th>Range</th>
                        <th>Status</th>
                        <th>Runs</th>
                        <th>Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((b) => (
                        <tr key={b.backtest_id} className="row-actions" style={{ cursor: "pointer" }} onClick={() => onSubmitted(b.backtest_id)}>
                          <td>{b.symbols.join(", ")}</td>
                          <td>{b.start_date} → {b.end_date}</td>
                          <td>
                            <StatusBadge status={b.status} />
                          </td>
                          <td>{b.completed_runs}/{b.total_runs}</td>
                          <td>{new Date(b.created_at).toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }
          </QueryBoundary>
        </div>
      )}
    </>
  );
}

function OptionsResearchTab({ onSubmitted, onOpen }: { onSubmitted: (id: string) => void; onOpen: (id: string) => void }) {
  const [subTab, setSubTab] = useState<"new" | "history">("new");
  const history = useOptionsResearchHistory();

  return (
    <>
      <div className="session-tabs" role="tablist" style={{ marginBottom: 16 }}>
        <button role="tab" aria-selected={subTab === "new"} className={`sm ${subTab === "new" ? "active" : ""}`} onClick={() => setSubTab("new")}>
          New Research
        </button>
        <button role="tab" aria-selected={subTab === "history"} className={`sm ${subTab === "history" ? "active" : ""}`} onClick={() => setSubTab("history")}>
          Research History
        </button>
      </div>

      {subTab === "new" && <OptionsResearchSetupForm onSubmitted={onSubmitted} />}
      {subTab === "history" && (
        <div className="card">
          <h2>AI Options Research History</h2>
          <QueryBoundary query={history} empty={(d) => isOptionsResearchDisabled(d) || d.items.length === 0}>
            {(data) =>
              isOptionsResearchDisabled(data) ? null : (
                <div className="table-wrap">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Underlying</th><th>Expiry</th><th>Status</th><th>Regime</th><th>Candidates</th><th>Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((r) => (
                        <tr key={r.research_id} className="row-actions" style={{ cursor: "pointer" }} onClick={() => onOpen(r.research_id)}>
                          <td>{r.underlying}</td>
                          <td>{r.expiry ?? "—"}</td>
                          <td><StatusBadge status={r.status} /></td>
                          <td>{r.market_regime_trend ?? "—"}</td>
                          <td>{r.candidate_count}</td>
                          <td>{new Date(r.created_at).toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }
          </QueryBoundary>
        </div>
      )}
    </>
  );
}

const PAGE_SIZE = 25;

function HistoryTab({ onOpen }: { onOpen: (researchId: string) => void }) {
  const [page, setPage] = useState(1);
  const [symbol, setSymbol] = useState("");
  const [status, setStatus] = useState<ResearchJobStatus | "">("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const history = useResearchHistory({
    page,
    page_size: PAGE_SIZE,
    symbol: symbol.trim() || undefined,
    status: status || undefined,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
  });

  if (history.data && isDisabled(history.data)) {
    return (
      <div className="state">
        AI Research Engine is currently disabled.
        <br />
        Enable <code>AI_RESEARCH_ENABLED</code> on the backend to use research functionality.
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Research History</h2>
      <p className="sub">Persisted in the database — history survives a backend restart.</p>
      <div className="form-row" style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
        <input
          placeholder="Filter by symbol"
          value={symbol}
          onChange={(e) => {
            setSymbol(e.target.value.toUpperCase());
            setPage(1);
          }}
        />
        <select
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as ResearchJobStatus | "");
            setPage(1);
          }}
        >
          <option value="">All statuses</option>
          <option value="QUEUED">Queued</option>
          <option value="RUNNING">Running</option>
          <option value="COMPLETED">Completed</option>
          <option value="FAILED">Failed</option>
        </select>
        <input
          type="date"
          value={dateFrom}
          onChange={(e) => {
            setDateFrom(e.target.value);
            setPage(1);
          }}
        />
        <input
          type="date"
          value={dateTo}
          onChange={(e) => {
            setDateTo(e.target.value);
            setPage(1);
          }}
        />
      </div>
      <QueryBoundary query={history} empty={(d) => isDisabled(d) || d.items.length === 0}>
        {(data) => {
          const page_ = isDisabled(data) ? { items: [], total: 0, page: 1, page_size: PAGE_SIZE } : data;
          const totalPages = Math.max(1, Math.ceil(page_.total / page_.page_size));
          return (
            <>
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Date</th>
                      <th>Depth</th>
                      <th>Status</th>
                      <th>Provider</th>
                      <th>Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page_.items.map((r) => (
                      <tr key={r.research_id} className="row-actions" onClick={() => onOpen(r.research_id)} style={{ cursor: "pointer" }}>
                        <td>{r.symbol}</td>
                        <td>{r.research_date}</td>
                        <td>{r.research_depth}</td>
                        <td>
                          <StatusBadge status={r.status} />
                        </td>
                        <td>{r.llm_provider ?? "—"}</td>
                        <td>{new Date(r.created_at).toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12 }}>
                <span className="sub">
                  Page {page_.page} of {totalPages} ({page_.total} total)
                </span>
                <div style={{ display: "flex", gap: 8 }}>
                  <button className="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                    Previous
                  </button>
                  <button className="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
                    Next
                  </button>
                </div>
              </div>
            </>
          );
        }}
      </QueryBoundary>
    </div>
  );
}

function MemoryTab() {
  const memory = useDecisionMemory();

  if (memory.data && isDisabled(memory.data)) {
    return (
      <div className="state">
        AI Research Engine is currently disabled.
        <br />
        Enable <code>AI_RESEARCH_ENABLED</code> on the backend to use research functionality.
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Decision Memory</h2>
      <p className="sub">TradingAgents' own decision log — past research decisions used as context for future runs.</p>
      <QueryBoundary query={memory} empty={(d) => !Array.isArray(d) || d.length === 0}>
        {(data) => {
          const rows = Array.isArray(data) ? data : [];
          return (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Ticker</th>
                    <th>Date</th>
                    <th>Rating</th>
                    <th>Status</th>
                    <th>Decision</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((m) => (
                    <tr key={m.memory_id}>
                      <td>{m.ticker}</td>
                      <td>{m.trade_date}</td>
                      <td>{m.rating ?? "—"}</td>
                      <td>{m.pending ? "Pending outcome" : "Settled"}</td>
                      <td style={{ maxWidth: 400, whiteSpace: "pre-wrap" }}>{m.decision ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
