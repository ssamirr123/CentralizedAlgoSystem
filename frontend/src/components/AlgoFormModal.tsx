import { useEffect, useState } from "react";
import type { AlgoListEntry, ServerListEntry } from "@/api/types";
import { ApiError } from "@/api/client";
import { TRADING_MODE } from "@/lib/config";

export interface AlgoFormValue {
  algo_id: string;
  server_id: string;
  script_path: string;
  enabled: boolean;
}

export function AlgoFormModal({
  mode,
  initial,
  servers,
  busy,
  error,
  onSubmit,
  onClose,
}: {
  mode: "create" | "edit";
  initial?: AlgoListEntry;
  servers: ServerListEntry[];
  busy: boolean;
  error: unknown;
  onSubmit: (v: AlgoFormValue) => void;
  onClose: () => void;
}) {
  const [v, setV] = useState<AlgoFormValue>({
    algo_id: initial?.algo_id ?? "",
    server_id: initial?.server_id ?? servers[0]?.server_id ?? "",
    script_path: initial?.script_path ?? "",
    enabled: initial?.enabled ?? true,
  });
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const nameInvalid = touched && !/^[A-Za-z0-9_-]+$/.test(v.algo_id.trim());
  const noServer = touched && !v.server_id;
  const canSubmit = !busy && !nameInvalid && !noServer && v.algo_id.trim() && v.server_id;
  const set = <K extends keyof AlgoFormValue>(k: K, val: AlgoFormValue[K]) => setV((p) => ({ ...p, [k]: val }));

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className={`mode-banner ${TRADING_MODE}`} style={{ marginBottom: 12 }}>
          <span className="pulse" />
          {TRADING_MODE === "live" ? "Live Trading" : "Paper Trading"}
        </div>
        <h3>{mode === "create" ? "Add Algorithm" : `Edit ${initial?.algo_id}`}</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 12.5, marginTop: 0 }}>
          {mode === "create"
            ? "Registers a strategy for management. Does not start it. A best-effort git sync to the target server runs after registration."
            : "Registration metadata only. Renaming isn't supported — identity is (name, server)."}
        </p>

        <div className="field">
          <label>Algorithm name</label>
          <input
            value={v.algo_id}
            autoFocus={mode === "create"}
            disabled={mode === "edit"}
            placeholder="example_strategy"
            className="mono"
            onChange={(e) => set("algo_id", e.target.value)}
            onBlur={() => setTouched(true)}
          />
          {nameInvalid && <div className="form-error">Letters, digits, underscore and hyphen only.</div>}
        </div>

        <div className="field">
          <label>Server</label>
          <select
            value={v.server_id}
            disabled={mode === "edit"}
            onChange={(e) => set("server_id", e.target.value)}
            onBlur={() => setTouched(true)}
          >
            <option value="">Select…</option>
            {servers.map((s) => (
              <option key={s.server_id} value={s.server_id}>
                {s.server_id} · {s.ec2_instance_id}
              </option>
            ))}
          </select>
          {noServer && <div className="form-error">Pick a server.</div>}
        </div>

        <div className="field">
          <label>Trading mode</label>
          <div className={`mode-banner ${TRADING_MODE}`} style={{ width: "fit-content" }}>
            <span className="pulse" />
            {TRADING_MODE === "live" ? "Live" : "Paper"} · locked
          </div>
        </div>

        <div className="field">
          <label>Script path (optional)</label>
          <input
            className="mono"
            value={v.script_path}
            placeholder={`trading/algos/${v.algo_id || "<name>"}/main.py`}
            onChange={(e) => set("script_path", e.target.value)}
          />
        </div>

        <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, color: "var(--text-dim)" }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={v.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
          />
          Enabled (included in scheduled start_all / update_all runs)
        </label>

        <div className="inline-note" style={{ marginTop: 12 }}>
          Strategy parameters (symbol, quantity, entry/exit, stop-loss, hedge) live in the repo —
          <code> trading/algos/{v.algo_id || "<name>"}/config.py</code> and <code>trading/.env</code> on the
          server — not in this record. The backend stores no per-algo config field.
        </div>

        {error instanceof ApiError && (
          <div className="form-error" style={{ marginTop: 10 }}>
            {error.status}: {error.detail}
          </div>
        )}

        <div className="actions">
          <button className="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className="primary"
            disabled={!canSubmit}
            onClick={() => {
              setTouched(true);
              if (canSubmit) onSubmit({ ...v, algo_id: v.algo_id.trim() });
            }}
          >
            {busy ? "Saving…" : mode === "create" ? "Create Algorithm" : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
