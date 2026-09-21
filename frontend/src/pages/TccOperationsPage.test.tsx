import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult } from "@/test/utils";
import { TccOperationsPage } from "./TccOperationsPage";
import * as hooks from "@/api/hooks";

vi.mock("@/api/hooks");

const SUMMARY = {
  generated_at: "2026-01-05T10:00:00+00:00",
  system: {
    ready: true, environment: "development", app_version: "1.2.3", git_sha: "abc123",
    deployment_id: "local-1", started_at: "2026-01-05T09:00:00+00:00", uptime_seconds: 3600, now: "2026-01-05T10:00:00+00:00",
  },
  workers: [
    {
      worker_id: "worker-cvn", name: "CombinedVWAP worker", status: "ONLINE", session_id: "s1",
      last_heartbeat_at: "2026-01-05T09:59:58+00:00", heartbeat_age_seconds: 2, heartbeat_timeout_seconds: 30,
      assigned_strategy_ids: ["CombinedVwapNifty"], version: "1.0", git_sha: "abc123", host_identity: "host-1",
      started_at: "2026-01-05T09:00:00+00:00", version_mismatch: false,
    },
  ],
  strategies: [
    {
      strategy_id: "CombinedVwapNifty", lifecycle_state: "STOPPED", runtime_state: "INACTIVE",
      account_authorization_state: "READ_ONLY", execution_active: false, worker_id: "worker-cvn",
      worker_status: "ONLINE", account_id: "ACC_CVN", market_data_status: "", last_market_data_at: "",
      last_cycle_at: "", last_result_summary: "", last_error: "",
    },
  ],
  accounts: [
    {
      account_id: "ACC_CVN", account_name: "Combined VWAP account", broker_id: "paper", enabled: true,
      authorization_state: "READ_ONLY", execution_mode: "SHADOW", assigned_strategy_ids: ["CombinedVwapNifty"],
      worker_ids: ["worker-cvn"],
    },
  ],
  portfolio_risk: { timestamp: "", daily_pnl: -500, gross_exposure: 12000, open_orders: 0, orders_today: 2, risk_status: "HEALTHY", limits: {} },
  safety: {
    kill_switch_engaged: false, kill_switch_engaged_by: "", kill_switch_reason: "",
    execution_mode_banner: "SHADOW", live_trading_disabled: true,
  },
  active_alerts: [],
};

describe("TccOperationsPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(hooks.useOperationsAudit).mockReturnValue(makeQueryResult({ data: [] }) as never);
  });

  it("shows a loading state before data arrives", () => {
    vi.mocked(hooks.useOperationsSummary).mockReturnValue(makeQueryResult({ isLoading: true }) as never);
    renderWithProviders(<TccOperationsPage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows OPERATIONS DATA UNAVAILABLE with last successful refresh time when the dashboard itself cannot reach the backend -- never renders stale data as current", () => {
    vi.mocked(hooks.useOperationsSummary).mockReturnValue({
      ...makeQueryResult({ isError: true, error: new Error("network down") }),
      dataUpdatedAt: new Date("2026-01-05T09:55:00Z").getTime(),
    } as never);
    renderWithProviders(<TccOperationsPage />);
    expect(screen.getByText("OPERATIONS DATA UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText(/Last successful refresh/)).toBeInTheDocument();
    // The stale summary table must not render underneath the unavailable banner.
    expect(screen.queryByText("CombinedVwapNifty")).not.toBeInTheDocument();
  });

  it("renders system, kill-switch, execution-mode, and deployment facts from authoritative backend state", () => {
    vi.mocked(hooks.useOperationsSummary).mockReturnValue(makeQueryResult({ data: SUMMARY }) as never);
    renderWithProviders(<TccOperationsPage />);
    expect(screen.getByText("READY")).toBeInTheDocument();
    expect(screen.getByText("DISENGAGED")).toBeInTheDocument();
    expect(screen.getAllByText("SHADOW").length).toBeGreaterThan(0);
    expect(screen.getByText("DISABLED")).toBeInTheDocument();
    expect(screen.getAllByText(/abc123/).length).toBeGreaterThan(0);
  });

  it("shows worker placement with heartbeat age and version", () => {
    vi.mocked(hooks.useOperationsSummary).mockReturnValue(makeQueryResult({ data: SUMMARY }) as never);
    renderWithProviders(<TccOperationsPage />);
    expect(screen.getAllByText("worker-cvn").length).toBeGreaterThan(0);
    expect(screen.getByText("2s ago")).toBeInTheDocument();
  });

  it("keeps lifecycle, runtime, authorization, and execution visually distinct for a strategy -- never a merged generic state", () => {
    vi.mocked(hooks.useOperationsSummary).mockReturnValue(makeQueryResult({ data: SUMMARY }) as never);
    renderWithProviders(<TccOperationsPage />);
    expect(screen.getByText("STOPPED")).toBeInTheDocument();
    expect(screen.getAllByText("INACTIVE").length).toBe(2); // runtime state AND execution column, distinct concepts
    expect(screen.getByText("READ_ONLY")).toBeInTheDocument();
    expect(screen.queryByText("ACTIVE")).not.toBeInTheDocument();
  });

  it("shows portfolio risk daily P&L and exposure, with unrealized P&L / delta-adjusted exposure explicitly documented as unsupported", () => {
    vi.mocked(hooks.useOperationsSummary).mockReturnValue(makeQueryResult({ data: SUMMARY }) as never);
    renderWithProviders(<TccOperationsPage />);
    expect(screen.getByText("-500")).toBeInTheDocument();
    expect(screen.getByText("12,000")).toBeInTheDocument();
    expect(screen.getByText(/UNSUPPORTED/)).toBeInTheDocument();
  });

  it("shows an explicit empty state for active alerts when there are none", () => {
    vi.mocked(hooks.useOperationsSummary).mockReturnValue(makeQueryResult({ data: SUMMARY }) as never);
    renderWithProviders(<TccOperationsPage />);
    expect(screen.getByText("No active alerts.")).toBeInTheDocument();
  });

  it("renders an active alert when one exists", () => {
    const withAlert = {
      ...SUMMARY,
      active_alerts: [{
        alert_id: "a1", code: "WORKER_OFFLINE", severity: "CRITICAL", category: "WORKER",
        source_type: "worker", source_id: "worker-cvn", message: "worker offline",
        raised_at: "2026-01-05T09:59:00+00:00", active: true, resolved_at: null,
      }],
    };
    vi.mocked(hooks.useOperationsSummary).mockReturnValue(makeQueryResult({ data: withAlert }) as never);
    renderWithProviders(<TccOperationsPage />);
    expect(screen.getByText("WORKER_OFFLINE")).toBeInTheDocument();
    expect(screen.getByText("worker offline")).toBeInTheDocument();
  });

  it("never renders a live-order or trading control on this page", () => {
    vi.mocked(hooks.useOperationsSummary).mockReturnValue(makeQueryResult({ data: SUMMARY }) as never);
    renderWithProviders(<TccOperationsPage />);
    for (const forbidden of [
      /^buy$/i, /^sell$/i, /place order/i, /close position/i, /authorize live/i, /go live/i, /live order/i,
      /^start strategy$/i, /^stop strategy$/i,
    ]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
