import { useState } from "react";
import { useRiskStatus, useSetKillSwitch } from "@/api/hooks";
import { ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { PageHeader } from "@/components/PageHeader";
import { Loading, ErrorState } from "@/components/States";
import { ConfirmDialog } from "@/components/ConfirmDialog";

const LIMIT_LABELS: Record<string, string> = {
  max_order_quantity: "Max order quantity",
  max_position_quantity: "Max position quantity",
  max_strategy_exposure: "Max strategy exposure",
  max_account_exposure: "Max account exposure",
  max_daily_loss: "Max daily loss",
  max_strategy_loss: "Max strategy loss",
  max_order_value: "Max order value",
};

export function TccRiskPage() {
  const { hasPermission } = useAuth();
  const canControlKillSwitch = hasPermission("ADMIN");

  const risk = useRiskStatus();
  const killSwitch = useSetKillSwitch();
  const [confirmEngage, setConfirmEngage] = useState(false);
  const [reason, setReason] = useState("");

  if (risk.isLoading) return <Loading />;
  if (risk.isError) return <ErrorState error={risk.error} onRetry={risk.refetch} />;
  const data = risk.data!;

  return (
    <>
      <PageHeader
        title="Risk"
        description="Centralized RiskManager (Phase 6) status — 14 always-evaluated checks, configured limits, and the process-wide kill switch (Phase 11)."
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <h2>Kill switch</h2>
        {data.kill_switch.engaged ? (
          <div className="inline-note warn" style={{ marginBottom: 12 }}>
            <strong>Engaged</strong> by {data.kill_switch.engaged_by || "—"} at {data.kill_switch.engaged_at || "—"}.
            {data.kill_switch.reason && <> Reason: {data.kill_switch.reason}.</>}
          </div>
        ) : (
          <div className="inline-note" style={{ marginBottom: 12 }}>Not engaged.</div>
        )}
        <p className="sub" style={{ marginTop: -4, marginBottom: 12 }}>
          Not yet auto-enforced by any live validation path (no strategy generates order intents today — see the
          Overview page) — this is a genuine, settable flag, not yet a functioning interlock.
        </p>
        {data.kill_switch.engaged ? (
          <button
            className="primary sm"
            disabled={!canControlKillSwitch || killSwitch.isPending}
            title={canControlKillSwitch ? undefined : "Requires ADMIN"}
            onClick={() => killSwitch.mutate({ engaged: false })}
          >
            Disengage
          </button>
        ) : (
          <button
            className="danger sm"
            disabled={!canControlKillSwitch}
            title={canControlKillSwitch ? undefined : "Requires ADMIN"}
            onClick={() => setConfirmEngage(true)}
          >
            Engage kill switch
          </button>
        )}
        {killSwitch.error instanceof ApiError && (
          <p className="form-error">{killSwitch.error.status}: {killSwitch.error.detail}</p>
        )}
      </div>

      <div className="card">
        <h2>Configured risk limits</h2>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Limit</th>
                <th className="num">Value</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.limits).map(([key, value]) => (
                <tr key={key}>
                  <td>{LIMIT_LABELS[key] ?? key}</td>
                  <td className="num">{value == null ? "not configured" : value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="sub" style={{ marginTop: 12 }}>
          A limit of "not configured" always passes that check — it is not the same as a limit of zero.
        </p>
      </div>

      {confirmEngage && (
        <ConfirmDialog
          open
          title="Engage the kill switch?"
          danger
          busy={killSwitch.isPending}
          confirmLabel="Engage"
          body={
            <>
              <p style={{ marginTop: 0 }}>
                This sets a process-wide flag visible to every future caller that reads it. It does not currently
                stop any live order (none are placed by this system today).
              </p>
              <div className="field">
                <label>Reason (optional)</label>
                <input value={reason} onChange={(e) => setReason(e.target.value)} />
              </div>
            </>
          }
          onConfirm={() => {
            killSwitch.mutate({ engaged: true, reason });
            setConfirmEngage(false);
            setReason("");
          }}
          onCancel={() => setConfirmEngage(false)}
        />
      )}
    </>
  );
}
