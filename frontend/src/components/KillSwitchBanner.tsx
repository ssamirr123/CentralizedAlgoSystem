/**
 * Informational only. The backend's kill switch (Phase 11) is not yet
 * wired into any live validation path (see docs/phase-11-control-center-
 * backend-report.md's Known Limitations) -- this banner reflects the
 * flag's current state without implying the UI itself is enforcing
 * anything beyond what the backend already does.
 */
export function KillSwitchBanner({ engaged, reason }: { engaged: boolean; reason?: string }) {
  if (!engaged) return null;
  return (
    <div className="inline-note warn" style={{ marginBottom: 14 }}>
      <strong>Kill switch engaged.</strong>
      {reason ? ` Reason: ${reason}.` : ""} See the Risk page to review or disengage.
    </div>
  );
}
