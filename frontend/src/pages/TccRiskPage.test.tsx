import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult, makeMutationResult } from "@/test/utils";
import { TccRiskPage } from "./TccRiskPage";
import * as hooks from "@/api/hooks";
import * as auth from "@/auth/AuthContext";

vi.mock("@/api/hooks");
vi.mock("@/auth/AuthContext");

const DISENGAGED = {
  kill_switch: { engaged: false, engaged_by: "", reason: "", engaged_at: "", disengaged_at: "" },
  limits: {
    max_order_quantity: 65,
    max_position_quantity: null,
    max_strategy_exposure: null,
    max_account_exposure: null,
    max_daily_loss: 2000,
    max_strategy_loss: 2000,
    max_order_value: 2000,
  },
  assigned_strategy_count: 1,
};

const PORTFOLIO_RISK = {
  timestamp: "2026-01-05T10:00:00+00:00",
  daily_pnl: 0,
  gross_exposure: 0,
  open_orders: 0,
  orders_today: 0,
  risk_status: "HEALTHY",
  limits: { max_portfolio_exposure: null },
};

describe("TccRiskPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // Phase 16.10: every test below focuses on the pre-existing
    // kill-switch/limits section -- give the new portfolio-risk hooks a
    // sane, non-crashing default so those tests don't need to know about
    // this phase's addition; the dedicated portfolio-risk tests below
    // override these explicitly.
    vi.mocked(hooks.usePortfolioRisk).mockReturnValue(makeQueryResult({ data: PORTFOLIO_RISK }) as never);
    vi.mocked(hooks.useAccountRisk).mockReturnValue(makeQueryResult({ data: [] }) as never);
    vi.mocked(hooks.useStrategyRisk).mockReturnValue(makeQueryResult({ data: [] }) as never);
  });

  it("shows a loading state before data arrives", () => {
    vi.mocked(hooks.useRiskStatus).mockReturnValue(makeQueryResult({ isLoading: true }) as never);
    vi.mocked(hooks.useSetKillSwitch).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccRiskPage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an error state on fetch failure", () => {
    vi.mocked(hooks.useRiskStatus).mockReturnValue(makeQueryResult({ isError: true, error: new Error("unreachable") }) as never);
    vi.mocked(hooks.useSetKillSwitch).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccRiskPage />);
    expect(screen.getByText("unreachable")).toBeInTheDocument();
  });

  it("renders CONFIGURED numeric limits distinctly from a not-configured (null) limit -- never shows null as 0/permissive", () => {
    vi.mocked(hooks.useRiskStatus).mockReturnValue(makeQueryResult({ data: DISENGAGED }) as never);
    vi.mocked(hooks.useSetKillSwitch).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccRiskPage />);
    expect(screen.getByText("65")).toBeInTheDocument(); // max_order_quantity, configured, unique
    expect(screen.getAllByText("2000").length).toBe(3); // max_daily_loss/max_strategy_loss/max_order_value
    expect(screen.getAllByText("not configured").length).toBeGreaterThan(0); // max_position_quantity etc.
  });

  it("disables the kill-switch engage control for an operator lacking ADMIN", () => {
    vi.mocked(hooks.useRiskStatus).mockReturnValue(makeQueryResult({ data: DISENGAGED }) as never);
    vi.mocked(hooks.useSetKillSwitch).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccRiskPage />);
    expect(screen.getByRole("button", { name: "Engage kill switch" })).toBeDisabled();
  });

  it("never mutates the kill switch merely by rendering -- mutate() is only wired to an explicit click+confirm", () => {
    const mutate = vi.fn();
    vi.mocked(hooks.useRiskStatus).mockReturnValue(makeQueryResult({ data: DISENGAGED }) as never);
    vi.mocked(hooks.useSetKillSwitch).mockReturnValue(makeMutationResult({ mutate }) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => true } as never);
    renderWithProviders(<TccRiskPage />);
    expect(mutate).not.toHaveBeenCalled();
  });

  it("never renders a live-order control on this page", () => {
    vi.mocked(hooks.useRiskStatus).mockReturnValue(makeQueryResult({ data: DISENGAGED }) as never);
    vi.mocked(hooks.useSetKillSwitch).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => true } as never);
    renderWithProviders(<TccRiskPage />);
    for (const forbidden of [/^buy$/i, /^sell$/i, /place order/i, /^start strategy$/i, /^stop strategy$/i, /authorize live/i, /enable live/i]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
  });

  it("shows portfolio-wide daily P&L, gross exposure, and risk status", () => {
    vi.mocked(hooks.useRiskStatus).mockReturnValue(makeQueryResult({ data: DISENGAGED }) as never);
    vi.mocked(hooks.useSetKillSwitch).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    vi.mocked(hooks.usePortfolioRisk).mockReturnValue(
      makeQueryResult({ data: { ...PORTFOLIO_RISK, daily_pnl: -500, gross_exposure: 12000, risk_status: "HEALTHY" } }) as never,
    );
    renderWithProviders(<TccRiskPage />);
    expect(screen.getByText("-500")).toBeInTheDocument();
    expect(screen.getByText("12,000")).toBeInTheDocument();
    expect(screen.getAllByText("HEALTHY").length).toBeGreaterThan(0);
  });

  it("shows a row per account and per strategy from the read-only portfolio-risk endpoints", () => {
    vi.mocked(hooks.useRiskStatus).mockReturnValue(makeQueryResult({ data: DISENGAGED }) as never);
    vi.mocked(hooks.useSetKillSwitch).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    vi.mocked(hooks.useAccountRisk).mockReturnValue(
      makeQueryResult({ data: [{ account_id: "ACC1", daily_pnl: 0, gross_exposure: 100, open_orders: 0, orders_today: 1, risk_status: "HEALTHY", limits: {} }] }) as never,
    );
    vi.mocked(hooks.useStrategyRisk).mockReturnValue(
      makeQueryResult({ data: [{ strategy_id: "CombinedVwapNifty", daily_pnl: 0, gross_exposure: 50, open_orders: 0, orders_today: 1, risk_status: "HEALTHY", limits: {} }] }) as never,
    );
    renderWithProviders(<TccRiskPage />);
    expect(screen.getByText("ACC1")).toBeInTheDocument();
    expect(screen.getByText("CombinedVwapNifty")).toBeInTheDocument();
  });
});
