import type { ReactNode } from "react";
import { TRADING_MODE } from "@/lib/config";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Every trading-control action routes through here. The dialog always
 * restates the trading mode so an operator can never fire a command
 * without seeing whether it is PAPER or LIVE.
 */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  danger,
  busy,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  if (!open) return null;
  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className={`mode-banner ${TRADING_MODE}`} style={{ marginBottom: 12 }}>
          <span className="pulse" />
          {TRADING_MODE === "live" ? "Live Trading" : "Paper Trading"}
        </div>
        <h3>{title}</h3>
        <div style={{ color: "var(--text-dim)", fontSize: 13 }}>{body}</div>
        <div className="actions">
          <button className="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button className={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
