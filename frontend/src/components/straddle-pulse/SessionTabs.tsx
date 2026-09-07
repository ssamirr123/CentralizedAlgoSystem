import type { StraddleSession } from "@/api/types";
import { formatDateLabel } from "./format";

export type SessionSelection = number | "whole-cycle";

export function SessionTabs({
  sessions, selected, onSelect,
}: {
  sessions: StraddleSession[];
  selected: SessionSelection;
  onSelect: (s: SessionSelection) => void;
}) {
  return (
    <div className="session-tabs">
      <button className={selected === "whole-cycle" ? "active" : ""} onClick={() => onSelect("whole-cycle")}>
        Whole cycle
      </button>
      {sessions.map((s) => (
        <button key={s.id} className={selected === s.id ? "active" : ""} onClick={() => onSelect(s.id)}>
          {label(s.trading_date)}
          {s.atm_strike != null && (
            <span style={{ marginLeft: 6, color: "var(--text-faint)", fontVariantNumeric: "tabular-nums" }}>
              ATM {s.atm_strike.toLocaleString("en-IN")}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

function label(isoDate: string): string {
  return formatDateLabel(isoDate);
}
