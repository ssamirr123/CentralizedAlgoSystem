import type { StraddleCycle } from "@/api/types";

/** Current / Previous / Older grouping, per underlying -- cycles are
 * already scoped to the selected underlying by the caller (useStraddleCycles). */
export function CycleSelector({
  cycles, selectedId, onSelect,
}: {
  cycles: StraddleCycle[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const sorted = [...cycles].sort((a, b) => (a.expiry_date < b.expiry_date ? 1 : -1));
  const active = sorted.find((c) => c.status === "ACTIVE");
  const completed = sorted.filter((c) => c.status !== "ACTIVE");
  const [previous, ...older] = completed;

  const label = (c: StraddleCycle) => `${c.cycle_start_date} -> ${c.expiry_date}`;

  return (
    <div className="field" style={{ minWidth: 240, marginBottom: 0 }}>
      <label>Cycle</label>
      <select value={selectedId ?? ""} onChange={(e) => onSelect(Number(e.target.value))}>
        {active && <option value={active.id}>Current Cycle · {label(active)}</option>}
        {previous && <option value={previous.id}>Previous Cycle · {label(previous)}</option>}
        {older.length > 0 && (
          <optgroup label="Older Cycles">
            {older.map((c) => (
              <option key={c.id} value={c.id}>
                {label(c)}
              </option>
            ))}
          </optgroup>
        )}
      </select>
    </div>
  );
}
