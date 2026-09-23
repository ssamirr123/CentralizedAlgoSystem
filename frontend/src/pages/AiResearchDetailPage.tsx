import { useParams, useNavigate } from "react-router-dom";
import { useResearchResult, useResearchStages, useResearchStatus, useResumeResearch } from "@/api/aiResearchHooks";
import { isDisabled } from "@/api/aiResearchTypes";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { StageProgress } from "@/components/ai-research/StageProgress";
import { ResearchReportView } from "@/components/ai-research/ResearchReportView";
import { MarketDataPanel } from "@/components/ai-research/MarketDataPanel";

const PROVIDER_ERROR_MESSAGE: Record<string, string> = {
  RATE_LIMIT: "LLM rate limit reached.",
  AUTH_ERROR: "Provider authentication failed.",
  MODEL_NOT_FOUND: "The configured model is not available on this provider.",
  PROVIDER_TIMEOUT: "Research timed out waiting on the provider.",
  TIMEOUT: "Research timed out.",
};

export function AiResearchDetailPage() {
  const { researchId = "" } = useParams<{ researchId: string }>();
  const navigate = useNavigate();
  const status = useResearchStatus(researchId);
  const resume = useResumeResearch();

  if (status.isLoading) return <Loading label="Loading research…" />;
  if (status.isError) return <div className="state error">Could not load this research job.</div>;
  if (!status.data) return null;
  if (isDisabled(status.data)) {
    return (
      <div className="state">
        AI Research Engine is currently disabled.
        <br />
        Enable <code>AI_RESEARCH_ENABLED</code> on the backend to use research functionality.
      </div>
    );
  }

  const job = status.data;
  const isActive = job.status === "QUEUED" || job.status === "RUNNING";

  async function handleResume() {
    const result = await resume.mutateAsync(researchId);
    if (!isDisabled(result)) navigate(`/ai-research/${result.new_research_id}`);
  }

  return (
    <>
      <PageHeader title={`${job.symbol} AI Research`} description="TradingAgents-powered multi-agent market research" />

      <div className="card" style={{ marginBottom: 16 }}>
        <dl className="kv">
          <dt>Research ID</dt>
          <dd>{job.research_id}</dd>
          <dt>Market</dt>
          <dd>{job.market === "INDIA" ? "India" : "US / Global"}</dd>
          <dt>Exchange</dt>
          <dd>{job.exchange}</dd>
          <dt>Currency</dt>
          <dd>{job.currency}</dd>
          <dt>Date</dt>
          <dd>{job.research_date}</dd>
          <dt>Research Depth</dt>
          <dd>{job.research_depth}</dd>
          <dt>Status</dt>
          <dd>
            <StatusBadge status={job.status} />
          </dd>
          <dt>Created</dt>
          <dd>{new Date(job.created_at).toLocaleString()}</dd>
          {job.started_at && (
            <>
              <dt>Started</dt>
              <dd>{new Date(job.started_at).toLocaleString()}</dd>
            </>
          )}
          {job.completed_at && (
            <>
              <dt>Completed</dt>
              <dd>{new Date(job.completed_at).toLocaleString()}</dd>
            </>
          )}
        </dl>
      </div>

      {job.status === "FAILED" && (
        <div className="card" style={{ marginBottom: 16 }} role="alert">
          <h3>Research Failed</h3>
          <p>{job.provider_error ? PROVIDER_ERROR_MESSAGE[job.provider_error] ?? job.error : job.error}</p>
          {job.provider_error && <p className="sub">Provider error category: {job.provider_error}</p>}
          <button onClick={handleResume} disabled={resume.isPending}>
            {resume.isPending ? "Resuming…" : "Resume Research"}
          </button>
          {resume.data && !isDisabled(resume.data) && (
            <p className="sub" style={{ marginTop: 8 }}>
              {resume.data.resumed_from_checkpoint
                ? "A new research job was started, which will attempt to continue from a saved checkpoint."
                : "A new research job was started from scratch — this run had no checkpoint saved, so it could not continue exactly where it stopped."}
            </p>
          )}
        </div>
      )}

      {(isActive || job.status === "FAILED") && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h2>Research Progress</h2>
          <StagesPanel researchId={researchId} jobStatus={job.status} />
        </div>
      )}

      {(job.status === "COMPLETED" || job.status === "FAILED") && (
        <ResultPanel researchId={researchId} status={job.status} symbol={job.symbol} />
      )}
    </>
  );
}

function StagesPanel({ researchId, jobStatus }: { researchId: string; jobStatus: string }) {
  const stages = useResearchStages(researchId, jobStatus);
  if (stages.isLoading) return <Loading label="Loading stage progress…" />;
  if (stages.isError || !stages.data) return <div className="state error">Could not load stage progress.</div>;
  if (isDisabled(stages.data)) return null;
  return <StageProgress stages={stages.data.stages} currentStage={stages.data.current_stage} />;
}

function ResultPanel({ researchId, status, symbol }: { researchId: string; status: string; symbol: string }) {
  const result = useResearchResult(researchId, status);
  if (result.isLoading) return <Loading label="Loading result…" />;
  if (result.isError || !result.data) return <div className="state error">Could not load research result.</div>;
  if (isDisabled(result.data)) return null;
  return (
    <>
      {result.data.market_data_provenance && <MarketDataPanel provenance={result.data.market_data_provenance} />}
      {result.data.report ? (
        <ResearchReportView report={result.data.report} symbol={symbol} />
      ) : (
        <div className="state">No report available for this run.</div>
      )}
    </>
  );
}
