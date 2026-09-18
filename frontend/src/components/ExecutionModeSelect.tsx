import { LIVE_EXECUTION_ENABLED } from "@/lib/config";
import type { ExecutionModeOption, ExecutionModeValue } from "@/api/types";

/**
 * The single place that decides whether LIVE is a selectable execution
 * mode anywhere in the Trading Control Center UI.
 *
 * LIVE_EXECUTION_ENABLED (frontend/src/lib/config.ts) is hard-wired to
 * `false` and is NOT driven by env or by any backend response field --
 * the same "backend safety gate" convention this codebase already
 * established for the legacy trading UI (see that constant's own
 * docstring). Phase 12 reuses it rather than inventing a second gate:
 * LIVE never appears as an option here unless that constant is flipped,
 * which requires a deliberate code change, not a config toggle. When a
 * future phase adds a real backend-driven safety-gate signal, this
 * component is the one place to update.
 *
 * Phase 14 added a second real-order-capable mode, LIVE_CANARY (tightly
 * capped live trading behind trading.common.live_canary.LiveCanaryGuard)
 * -- it is gated by the exact same LIVE_EXECUTION_ENABLED constant as
 * LIVE, for the same reason: both place real orders, so neither may
 * appear as a selectable option in this UI until that single, hard-coded
 * safety gate is deliberately flipped.
 */
const _REAL_ORDER_MODES = new Set<ExecutionModeValue>(["LIVE", "LIVE_CANARY"]);
export function ExecutionModeSelect({
  modes,
  value,
  onChange,
  disabled,
  allowAccountDefault = true,
}: {
  modes: ExecutionModeOption[] | undefined;
  value: ExecutionModeValue | "";
  onChange: (v: ExecutionModeValue | "") => void;
  disabled?: boolean;
  allowAccountDefault?: boolean;
}) {
  const options = (modes ?? []).filter((m) => !_REAL_ORDER_MODES.has(m.value) || LIVE_EXECUTION_ENABLED);
  return (
    <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value as ExecutionModeValue | "")}>
      {allowAccountDefault && <option value="">Account default</option>}
      {options.map((m) => (
        <option key={m.value} value={m.value}>
          {m.value}
        </option>
      ))}
    </select>
  );
}
