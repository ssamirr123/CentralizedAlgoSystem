import type { NodeStage } from "@/api/aiResearchTypes";

// Section 9/10: renders ONLY stages the backend actually returned (an
// unselected analyst is simply absent from the array -- Section 11/7 --
// never shown as perpetually pending). COMPLETED is a real observed
// LangGraph event; RUNNING is the backend's own inference (the next
// not-yet-completed node) -- labelled "current stage", never a fabricated
// percentage, and never implying an exact start timestamp is known.
const ICON: Record<NodeStage["status"], string> = {
  COMPLETED: "✓", // check
  RUNNING: "●", // filled circle
  FAILED: "✕", // cross
  PENDING: "○", // hollow circle
  SKIPPED: "–", // en dash
};

const LABEL: Record<NodeStage["status"], string> = {
  COMPLETED: "Completed",
  RUNNING: "Current stage",
  FAILED: "Failed",
  PENDING: "Pending",
  SKIPPED: "Skipped",
};

export function StageProgress({ stages, currentStage }: { stages: NodeStage[]; currentStage: string | null }) {
  if (stages.length === 0) {
    return <div className="state">No stage information yet.</div>;
  }
  return (
    <div>
      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {stages.map((s) => (
          <li
            key={s.name}
            style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}
            aria-current={s.name === currentStage ? "step" : undefined}
          >
            <span aria-hidden="true">{ICON[s.status]}</span>
            <span>{s.name}</span>
            <span className="sub" style={{ marginLeft: "auto" }}>
              {LABEL[s.status]}
              {s.duration_ms != null ? ` · ${(s.duration_ms / 1000).toFixed(1)}s` : ""}
            </span>
          </li>
        ))}
      </ul>
      {currentStage && (
        <p className="sub" style={{ marginTop: 8 }}>
          Current / latest stage: <strong>{currentStage}</strong> (inferred from the most recently completed step —
          not an exact real-time start timestamp).
        </p>
      )}
    </div>
  );
}
