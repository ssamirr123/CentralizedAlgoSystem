import { useParams } from "react-router-dom";
import {
  useOptionsResearchResult, useOptionsResearchStages, useOptionsResearchStatus,
} from "@/api/aiOptionsResearchHooks";
import { isDisabled } from "@/api/aiOptionsResearchTypes";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { CandidateCard, CandidateComparisonTable } from "@/components/ai-options-research/CandidateCard";

export function AiOptionsResearchDetailPage() {
  const { researchId = "" } = useParams<{ researchId: string }>();
  const status = useOptionsResearchStatus(researchId);

  if (status.isLoading) return <Loading label="Loading research…" />;
  if (status.isError) return <div className="state error">Could not load this research job.</div>;
  if (!status.data) return null;
  if (isDisabled(status.data)) {
    return <div className="state">AI Research Engine is currently disabled.</div>;
  }

  const job = status.data;
  const isActive = job.status === "QUEUED" || job.status === "RUNNING";

  return (
    <>
      <PageHeader title={`${job.underlying} AI Options Research`} description="Multi-agent options research -- research only, no order execution" />

      <div className="card" style={{ marginBottom: 16 }}>
        <dl className="kv">
          <dt>Research ID</dt><dd>{job.research_id}</dd>
          <dt>Underlying</dt><dd>{job.underlying}</dd>
          <dt>Status</dt><dd><StatusBadge status={job.status} /></dd>
          <dt>Evidence Quality</dt><dd>{job.evidence_quality ?? "—"}</dd>
          <dt>Snapshot ID</dt><dd>{job.research_snapshot_id ?? "—"}</dd>
          <dt>Created</dt><dd>{new Date(job.created_at).toLocaleString()}</dd>
        </dl>
      </div>

      {job.status === "FAILED" && (
        <div className="card" role="alert" style={{ marginBottom: 16 }}>
          <h3>Research Failed</h3>
          <p>{job.error}</p>
          {job.provider_error && <p className="sub">Category: {job.provider_error}</p>}
        </div>
      )}

      {isActive && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h2>Progress</h2>
          <StagesPanel researchId={researchId} jobStatus={job.status} />
        </div>
      )}

      {(job.status === "COMPLETED" || job.status === "FAILED") && (
        <ResultPanel researchId={researchId} status={job.status} />
      )}
    </>
  );
}

function StagesPanel({ researchId, jobStatus }: { researchId: string; jobStatus: string }) {
  const stages = useOptionsResearchStages(researchId, jobStatus);
  if (stages.isLoading) return <Loading label="Loading progress…" />;
  if (stages.isError || !stages.data || isDisabled(stages.data)) return null;
  return (
    <ul>
      {stages.data.stages.map((s) => (
        <li key={s.name}>
          {s.status === "COMPLETED" ? "✓" : s.status === "RUNNING" ? "→" : s.status === "SKIPPED" ? "⊘" : "○"} {s.name}
        </li>
      ))}
    </ul>
  );
}

function ResultPanel({ researchId, status }: { researchId: string; status: string }) {
  const result = useOptionsResearchResult(researchId, status);
  if (result.isLoading) return <Loading label="Loading result…" />;
  if (result.isError || !result.data || isDisabled(result.data)) return <div className="state error">Could not load research result.</div>;
  const report = result.data.report;
  if (!report) return <div className="state">No report available.</div>;

  return (
    <>
      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Market Overview</h2>
        <p>{report.market_overview}</p>
        {report.market_regime.trend && (
          <dl className="kv">
            <dt>Trend</dt><dd>{report.market_regime.trend}</dd>
            <dt>Volatility Regime</dt><dd>{report.market_regime.volatility_regime}</dd>
            <dt>Market Structure</dt><dd>{report.market_regime.market_structure}</dd>
            <dt>Confidence</dt><dd>{report.market_regime.confidence}</dd>
          </dl>
        )}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Options Market Structure</h2>
        <p>{report.options_market_structure.summary ?? report.options_market_structure.reason}</p>
        <h3>Volatility Analysis</h3>
        <p>{report.volatility_analysis.interpretation}</p>
        <h3>OI / Positioning Analysis</h3>
        <p>{report.oi_positioning_analysis.interpretation}</p>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Bull Case</h2>
        <p>{report.bull_case.thesis}</p>
        <h2>Bear Case</h2>
        <p>{report.bear_case.thesis}</p>
      </div>

      <h2>Candidate Strategy Structures</h2>
      {report.candidates.map((c, i) => <CandidateCard key={i} candidate={c} index={i} />)}
      <CandidateComparisonTable candidates={report.candidates} />

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Invalidation Conditions</h2>
        <ul>{report.invalidation_conditions.map((c, i) => <li key={i}>{c}</li>)}</ul>
      </div>

      <div className="card">
        <h2>Final Research Summary</h2>
        <p>{report.final_summary}</p>
      </div>
    </>
  );
}
