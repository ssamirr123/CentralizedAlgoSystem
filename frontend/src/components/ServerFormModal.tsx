import { useEffect, useState } from "react";
import type { ServerListEntry } from "@/api/types";
import { ApiError } from "@/api/client";
import { TRADING_MODE } from "@/lib/config";

const REGIONS = [
  "ap-south-1",
  "ap-south-2",
  "ap-southeast-1",
  "ap-southeast-2",
  "us-east-1",
  "us-west-2",
  "eu-west-1",
  "eu-central-1",
];
const OS_OPTIONS = ["linux", "windows"];

export interface ServerFormValue {
  server_id: string;
  ec2_instance_id: string;
  region: string;
  os: string;
  repo_path: string;
  auto_provision: boolean;
}

const EC2_ID_RE = /^i-[0-9a-f]{8,17}$/;

export function ServerFormModal({
  mode,
  initial,
  busy,
  error,
  onSubmit,
  onClose,
}: {
  mode: "create" | "edit";
  initial?: ServerListEntry;
  busy: boolean;
  error: unknown;
  onSubmit: (v: ServerFormValue) => void;
  onClose: () => void;
}) {
  const [v, setV] = useState<ServerFormValue>({
    server_id: initial?.server_id ?? "",
    ec2_instance_id: initial?.ec2_instance_id ?? "",
    region: initial?.region ?? "ap-south-1",
    os: initial?.os ?? "linux",
    repo_path: initial?.repo_path ?? "/trading-app",
    auto_provision: false,
  });
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const idInvalid = touched && !EC2_ID_RE.test(v.ec2_instance_id.trim());
  const nameInvalid = touched && v.server_id.trim().length === 0;
  const canSubmit = !busy && !idInvalid && !nameInvalid && v.server_id.trim() && v.ec2_instance_id.trim();

  const set = <K extends keyof ServerFormValue>(k: K, val: ServerFormValue[K]) =>
    setV((prev) => ({ ...prev, [k]: val }));

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className={`mode-banner ${TRADING_MODE}`} style={{ marginBottom: 12 }}>
          <span className="pulse" />
          {TRADING_MODE === "live" ? "Live Trading" : "Paper Trading"}
        </div>
        <h3>{mode === "create" ? "Add Server" : `Edit ${initial?.server_id}`}</h3>
        <p style={{ color: "var(--text-dim)", fontSize: 12.5, marginTop: 0 }}>
          {mode === "create"
            ? "Registers an EC2 server with the control plane. Does not start the instance."
            : "Updates control-plane metadata only. Does not restart the instance."}
        </p>

        <div className="field">
          <label>Server name</label>
          <input
            value={v.server_id}
            autoFocus
            placeholder="strategy-01"
            onChange={(e) => set("server_id", e.target.value)}
            onBlur={() => setTouched(true)}
          />
          {nameInvalid && <div className="form-error">Server name is required.</div>}
        </div>

        <div className="field">
          <label>EC2 instance ID</label>
          <input
            value={v.ec2_instance_id}
            placeholder="i-0123456789abcdef0"
            className="mono"
            onChange={(e) => set("ec2_instance_id", e.target.value)}
            onBlur={() => setTouched(true)}
          />
          {idInvalid && <div className="form-error">Expected an EC2 id like i-0123456789abcdef0.</div>}
        </div>

        <div className="field">
          <label>AWS region</label>
          <select value={v.region} onChange={(e) => set("region", e.target.value)}>
            {REGIONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>Operating system</label>
          <select value={v.os} onChange={(e) => set("os", e.target.value)}>
            {OS_OPTIONS.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>Repo path</label>
          <input className="mono" value={v.repo_path} onChange={(e) => set("repo_path", e.target.value)} />
        </div>

        {mode === "create" && (
          <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, color: "var(--text-dim)" }}>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              checked={v.auto_provision}
              onChange={(e) => set("auto_provision", e.target.checked)}
            />
            Auto-provision (attach IAM profile, reboot, install deps via Lambda)
          </label>
        )}

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
              if (canSubmit) onSubmit({ ...v, server_id: v.server_id.trim(), ec2_instance_id: v.ec2_instance_id.trim() });
            }}
          >
            {busy ? "Saving…" : mode === "create" ? "Create Server" : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
