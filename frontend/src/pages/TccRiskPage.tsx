import { useState } from "react";
import { useAccountRisk, usePortfolioRisk, useRiskStatus, useSetKillSwitch, useStrategyRisk } from "@/api/hooks";
import { ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { PageHeader } from "@/components/PageHeader";
import { Loading, ErrorState, QueryBoundary } from "@/components/States";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import type { AccountRisk, StrategyRisk } from "@/api/types";

function fmt(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function ExposureRow({ label, dailyPnl, grossExposure, openOrders, ordersToday, status }: {
  label: string; dailyPnl: number; grossExposure: number; openOrders: number; ordersToday: number; status: string;
}) {
  return (
    <tr>
      <td>{label}</td>
      <td className="num" style={{ color: dailyPnl < 0 ? "var(--danger, #b42318)" : undefined }}>{fmt(dailyPnl)}</td>
      <td className="num">{fmt(grossExposure)}</td>
      <td className="num">{openOrders}</td>
      <td className="num">{ordersToday}</td>
      <td><span className="badge">{status}</span></td>
    </tr>
  );
}

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
  const portfolioRisk = usePortfolioRisk();
  const accountRisk = useAccountRisk();
  const strategyRisk = useStrategyRisk();
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

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Portfolio risk (Phase 16.10)</h2>
        <p className="sub" style={{ marginTop: -4 }}>
          Central, additive risk layer for worker-submitted orders — sits in front of the RiskManager above, never
          replaces it. Exposure is notional/quantity based (quantity × price), not delta-adjusted. Daily P&amp;L is
          realized-only (from closed shadow fills); no BUY/SELL/PLACE ORDER/AUTHORIZE LIVE control exists on this
          page or anywhere in this API.
        </p>
        <QueryBoundary query={portfolioRisk}>
          {(data) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Scope</th>
                    <th className="num">Daily P&amp;L</th>
                    <th className="num">Gross exposure</th>
                    <th className="num">Open orders</th>
                    <th className="num">Orders today</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  <ExposureRow
                    label="Portfolio"
                    dailyPnl={data.daily_pnl}
                    grossExposure={data.gross_exposure}
                    openOrders={data.open_orders}
                    ordersToday={data.orders_today}
                    status={data.risk_status}
                  />
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>

        <h3 style={{ marginTop: 20 }}>Accounts</h3>
        <QueryBoundary query={accountRisk} empty={(d) => d.length === 0}>
          {(rows: AccountRisk[]) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Account</th>
                    <th className="num">Daily P&amp;L</th>
                    <th className="num">Gross exposure</th>
                    <th className="num">Open orders</th>
                    <th className="num">Orders today</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <ExposureRow
                      key={r.account_id}
                      label={r.account_id}
                      dailyPnl={r.daily_pnl}
                      grossExposure={r.gross_exposure}
                      openOrders={r.open_orders}
                      ordersToday={r.orders_today}
                      status={r.risk_status}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>

        <h3 style={{ marginTop: 20 }}>Strategies</h3>
        <QueryBoundary query={strategyRisk} empty={(d) => d.length === 0}>
          {(rows: StrategyRisk[]) => (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th className="num">Daily P&amp;L</th>
                    <th className="num">Gross exposure</th>
                    <th className="num">Open orders</th>
                    <th className="num">Orders today</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <ExposureRow
                      key={r.strategy_id}
                      label={r.strategy_id}
                      dailyPnl={r.daily_pnl}
                      grossExposure={r.gross_exposure}
                      openOrders={r.open_orders}
                      ordersToday={r.orders_today}
                      status={r.risk_status}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryBoundary>
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
