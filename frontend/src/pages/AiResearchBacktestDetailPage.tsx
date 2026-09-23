import { useParams, useNavigate } from "react-router-dom";
import {
  useBacktestDetail,
  useBacktestRuns,
  useBacktestStatus,
  useCancelBacktest,
  useResumeBacktest,
} from "@/api/aiResearchBacktestHooks";
import { isDisabled } from "@/api/aiResearchTypes";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { DataQualityPanel } from "@/components/ai-research/DataQualityPanel";

function pct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

function fmtReturn(n: number | null): string {
  if (n === null) return "—";
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(1)}%`;
}

export function AiResearchBacktestDetailPage() {
  const { backtestId = "" } = useParams<{ backtestId: string }>();
  const navigate = useNavigate();
  const status = useBacktestStatus(backtestId);
  const resume = useResumeBacktest();
  const cancel = useCancelBacktest();

  if (status.isLoading) return <Loading label="Loading backtest…" />;
  if (status.isError) return <div className="state error">Could not load this backtest.</div>;
  if (!status.data) return null;
  if (isDisabled(status.data)) {
    return (
      <div className="state">
        AI Research Engine is currently disabled.
        <br />
        Enable <code>AI_RESEARCH_ENABLED</code> on the backend to use backtesting.
      </div>
    );
  }

  const job = status.data;
  const isActive = job.status === "QUEUED" || job.status === "RUNNING";

  return (
    <>
      <PageHeader title="AI Research Backtest" description="Historical decisions vs. what actually happened afterward" />

      <div className="card" style={{ marginBottom: 16 }}>
        <dl className="kv">
          <dt>Backtest ID</dt>
          <dd>{job.backtest_id}</dd>
          <dt>Status</dt>
          <dd>
            <StatusBadge status={job.status} />
          </dd>
          <dt>Total Runs</dt>
          <dd>{job.total_runs}</dd>
          <dt>Completed</dt>
          <dd>{job.completed_runs}</dd>
          <dt>Failed</dt>
          <dd>{job.failed_runs}</dd>
          <dt>Remaining</dt>
          <dd>{job.remaining_runs}</dd>
          {job.current_symbol && (
            <>
              <dt>Currently Processing</dt>
              <dd>
                {job.current_symbol} {job.current_date}
              </dd>
            </>
          )}
        </dl>

        {isActive && (
          <div style={{ marginTop: 12 }}>
            <div className="sub">Progress: {pct(job.progress)}</div>
            <div style={{ background: "var(--surface-2, #222)", borderRadius: 4, height: 8, overflow: "hidden" }}>
              <div
                style={{
                  width: pct(job.progress), height: "100%",
                  background: "var(--accent, #4a9eff)", transition: "width 0.3s",
                }}
              />
            </div>
            <button className="sm ghost" style={{ marginTop: 8 }} onClick={() => cancel.mutate(backtestId)} disabled={cancel.isPending}>
              {cancel.isPending ? "Cancelling…" : "Cancel"}
            </button>
          </div>
        )}

        {(job.status === "FAILED" || job.status === "CANCELLED") && (
          <div style={{ marginTop: 12 }}>
            {job.error && <p className="form-error">{job.error}</p>}
            <button onClick={() => resume.mutate(backtestId)} disabled={resume.isPending}>
              {resume.isPending ? "Resuming…" : "Resume (continue unfinished cells only)"}
            </button>
            {resume.data && !isDisabled(resume.data) && (
              <p className="sub" style={{ marginTop: 8 }}>
                Resumed — {resume.data.resumed_pending_cells} cell(s) remaining will be (re)run. Already-completed
                cells are never re-run.
              </p>
            )}
          </div>
        )}
      </div>

      {job.status === "COMPLETED" && <MetricsPanel backtestId={backtestId} />}

      <RunsPanel backtestId={backtestId} jobStatus={job.status} onOpenResearch={(id) => navigate(`/ai-research/${id}`)} />

      <div style={{ marginTop: 16 }}>
        <DataQualityPanel />
      </div>
    </>
  );
}

function MetricsPanel({ backtestId }: { backtestId: string }) {
  const detail = useBacktestDetail(backtestId, "COMPLETED");
  if (detail.isLoading) return <Loading label="Loading metrics…" />;
  if (detail.isError || !detail.data || isDisabled(detail.data) || !detail.data.metrics) return null;
  const m = detail.data.metrics;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2>{m.label}</h2>
      <p className="sub">
        Decision quality against what actually happened over the next {m.holding_days} trading days — not a
        trading-strategy or portfolio-P&amp;L result.
      </p>
      <dl className="kv">
        <dt>Total Decisions</dt>
        <dd>{m.total_decisions}</dd>
        <dt>Resolved</dt>
        <dd>{m.resolved}</dd>
        <dt>Pending Evaluation</dt>
        <dd>{m.pending_evaluation}</dd>
        <dt>Unscored (REVIEW)</dt>
        <dd>{m.unscored}</dd>
        <dt>Overall Mean Alpha</dt>
        <dd>{fmtReturn(m.overall_mean_alpha)}</dd>
        <dt>Overall Median Alpha</dt>
        <dd>{fmtReturn(m.overall_median_alpha)}</dd>
        {m.positive_decision_rate !== null && (
          <>
            <dt>Positive Decision Rate</dt>
            <dd>{pct(m.positive_decision_rate)}</dd>
          </>
        )}
      </dl>

      <div className="table-wrap" style={{ marginTop: 12 }}>
        <table className="data">
          <thead>
            <tr>
              <th>Decision</th>
              <th>Count</th>
              <th>Directional Hit Rate</th>
              <th>Mean Alpha</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(m.by_decision).map(([decision, acc]) => (
              <tr key={decision}>
                <td>{decision}</td>
                <td>{acc.count}</td>
                <td>{acc.hit_rate === null ? "— (no direction claimed)" : pct(acc.hit_rate)}</td>
                <td>{fmtReturn(acc.mean_alpha)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RunsPanel({
  backtestId, jobStatus, onOpenResearch,
}: {
  backtestId: string; jobStatus: string; onOpenResearch: (researchId: string) => void;
}) {
  const runs = useBacktestRuns(backtestId, jobStatus);
  if (runs.isLoading) return <Loading label="Loading individual runs…" />;
  if (runs.isError || !runs.data || isDisabled(runs.data)) return <div className="state error">Could not load runs.</div>;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2>Individual Runs</h2>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Date</th>
              <th>Symbol</th>
              <th>Decision</th>
              <th>{runs.data.items[0]?.holding_days ?? ""}D Return</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.items.map((cell) => (
              <tr
                key={`${cell.symbol}-${cell.cell_date}`}
                className={cell.research_id ? "row-actions" : ""}
                style={cell.research_id ? { cursor: "pointer" } : undefined}
                onClick={() => cell.research_id && onOpenResearch(cell.research_id)}
              >
                <td>{cell.cell_date}</td>
                <td>{cell.symbol}</td>
                <td>{cell.normalized_decision ?? "—"}</td>
                <td>
                  {cell.evaluation_status === "RESOLVED" ? fmtReturn(cell.alpha_return) : cell.status === "COMPLETED" ? "pending" : "—"}
                </td>
                <td>
                  <StatusBadge status={cell.status} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {runs.data.items.length === 0 && <div className="state">No runs yet.</div>}
    </div>
  );
}
