import type { StraddleCycle } from "@/api/types";
import { UnderlyingToggle } from "./UnderlyingToggle";

export function CycleHeader({
  underlying, onUnderlyingChange, cycle, sessionCount,
}: {
  underlying: string;
  onUnderlyingChange: (u: string) => void;
  cycle: StraddleCycle | undefined;
  sessionCount: number;
}) {
  return (
    <div className="toolbar" style={{ alignItems: "center", justifyContent: "space-between" }}>
      <div>
        <div style={{ fontSize: 18, fontWeight: 700 }}>
          Straddle Pulse <span style={{ color: "var(--text-faint)" }}>· Weekly</span>
        </div>
        <div style={{ fontSize: 12.5, color: "var(--text-dim)" }}>
          {cycle
            ? `Running ${underlying} weekly · ${cycle.cycle_start_date} → expiry ${cycle.expiry_date} · ${sessionCount} sessions`
            : `No active ${underlying} cycle yet`}
        </div>
      </div>
      <UnderlyingToggle value={underlying} onChange={onUnderlyingChange} />
    </div>
  );
}
