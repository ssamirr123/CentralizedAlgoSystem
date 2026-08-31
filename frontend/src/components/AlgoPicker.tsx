import { useEffect, useMemo } from "react";
import { useAlgos } from "@/api/hooks";

export interface AlgoRef {
  algoId: string;
  serverId: string;
}

/**
 * Shared selector for the many endpoints keyed by (algo_id, server_id).
 * Options come from GET /api/algos. `value` is the encoded "algo|server".
 */
export function AlgoPicker({
  value,
  onChange,
  label = "Strategy",
}: {
  value: AlgoRef | null;
  onChange: (v: AlgoRef | null) => void;
  label?: string;
}) {
  const { data, isLoading, isError } = useAlgos();

  const options = useMemo(
    () =>
      (data ?? []).map((a) => ({
        key: `${a.algo_id}|${a.server_id}`,
        algoId: a.algo_id,
        serverId: a.server_id,
        label: `${a.algo_id}  ·  ${a.server_id}`,
      })),
    [data],
  );

  // Auto-select the first option once, so dependent screens render data
  // immediately instead of an empty picker.
  useEffect(() => {
    if (!value && options.length > 0) {
      onChange({ algoId: options[0].algoId, serverId: options[0].serverId });
    }
  }, [value, options, onChange]);

  const current = value ? `${value.algoId}|${value.serverId}` : "";

  return (
    <div className="field">
      <label htmlFor="algo-picker">{label}</label>
      <select
        id="algo-picker"
        value={current}
        disabled={isLoading || isError || options.length === 0}
        onChange={(e) => {
          const opt = options.find((o) => o.key === e.target.value);
          onChange(opt ? { algoId: opt.algoId, serverId: opt.serverId } : null);
        }}
      >
        {isLoading && <option>Loading…</option>}
        {isError && <option>Failed to load strategies</option>}
        {!isLoading && !isError && options.length === 0 && <option>No strategies registered</option>}
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}
