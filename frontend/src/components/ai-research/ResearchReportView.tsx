import { useState } from "react";
import type { ResearchReport } from "@/api/aiResearchTypes";
import { ResearchOnlyWarning } from "./ResearchOnlyWarning";

type Tab = "overview" | "analysts" | "debate" | "trader" | "risk" | "portfolio" | "raw";

const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "analysts", label: "Analysts" },
  { key: "debate", label: "Bull vs Bear" },
  { key: "trader", label: "Trader" },
  { key: "risk", label: "Risk" },
  { key: "portfolio", label: "Portfolio Manager" },
  { key: "raw", label: "Raw Data" },
];

function Section({ title, text }: { title: string; text: string | null }) {
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <h3>{title}</h3>
      {text ? <p style={{ whiteSpace: "pre-wrap" }}>{text}</p> : <p className="sub">Not available for this run.</p>}
    </div>
  );
}

export function ResearchReportView({ report, symbol }: { report: ResearchReport; symbol: string }) {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <div>
      <div className="session-tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            className={`sm ${tab === t.key ? "active" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div style={{ marginTop: 12 }}>
        {tab === "overview" && (
          <div className="card">
            <h3>Final Research Decision</h3>
            <p style={{ fontSize: "1.4em", fontWeight: 600 }}>{report.final_trade_decision ? report.signal ?? "REVIEW" : "—"}</p>
            <p style={{ whiteSpace: "pre-wrap" }}>{report.final_trade_decision ?? "Not available for this run."}</p>
            <ResearchOnlyWarning />
          </div>
        )}

        {tab === "analysts" && (
          <div>
            <Section title={`Technical / Market Analyst — ${symbol}`} text={report.market_analysis} />
            <Section title="Fundamentals Analyst" text={report.fundamentals_analysis} />
            <Section title="News Analyst" text={report.news_analysis} />
            <Section title="Sentiment Analyst" text={report.sentiment_analysis} />
          </div>
        )}

        {tab === "debate" && (
          <div>
            <div className="grid cols-2">
              <Section title="Bull Case" text={report.bull_case} />
              <Section title="Bear Case" text={report.bear_case} />
            </div>
            <Section title="Research Debate — Manager Decision" text={report.research_manager_decision} />
          </div>
        )}

        {tab === "trader" && (
          <div>
            <div className="card">
              <h3>AI Research Decision</h3>
              <p style={{ whiteSpace: "pre-wrap" }}>{report.trader_plan ?? "Not available for this run."}</p>
            </div>
            <ResearchOnlyWarning />
          </div>
        )}

        {tab === "risk" && (
          <div>
            <Section title="Aggressive Perspective" text={report.risk_aggressive} />
            <Section title="Conservative Perspective" text={report.risk_conservative} />
            <Section title="Neutral Perspective" text={report.risk_neutral} />
            <Section title="Risk Team Final Recommendation" text={report.risk_judge_decision} />
          </div>
        )}

        {tab === "portfolio" && (
          <div>
            <div className="card">
              <h3>Portfolio Manager</h3>
              <h4>Final Research Decision</h4>
              <p style={{ fontSize: "1.2em", fontWeight: 600 }}>{report.signal ?? "REVIEW"}</p>
              <h4>Reasoning</h4>
              <p style={{ whiteSpace: "pre-wrap" }}>{report.final_trade_decision ?? "Not available for this run."}</p>
            </div>
            <ResearchOnlyWarning />
          </div>
        )}

        {tab === "raw" && (
          <div className="card">
            <h3>Raw Data</h3>
            <p className="sub">Normalized backend output. Never includes API keys, tokens, or filesystem paths.</p>
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, overflowX: "auto" }}>
              {JSON.stringify(report, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
