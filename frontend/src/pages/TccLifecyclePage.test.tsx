import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult } from "@/test/utils";
import { TccLifecyclePage } from "./TccLifecyclePage";
import * as hooks from "@/api/hooks";

vi.mock("@/api/hooks");

const ROWS = [
  {
    strategy_id: "DoubleStraddelAlgo", assignment_id: "DoubleStraddelAlgo", account_id: "ACC_B",
    strategy_status: "enabled" as const, lifecycle_state: "READY" as const,
    account_authorization_state: "READ_ONLY" as const, live_authorized: false, execution_active: false,
    last_transition_at: "", last_heartbeat_at: "", last_error: "", assignment_exists: true, blocking_reasons: [],
  },
  {
    strategy_id: "CombinedVwapNifty", assignment_id: null, account_id: null,
    strategy_status: "disabled" as const, lifecycle_state: "STOPPED" as const,
    account_authorization_state: null, live_authorized: false, execution_active: false,
    last_transition_at: "", last_heartbeat_at: "", last_error: "", assignment_exists: false,
    blocking_reasons: ["no assignment exists for this strategy"],
  },
];

describe("TccLifecyclePage", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows a loading state before data arrives", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(makeQueryResult({ isLoading: true }) as never);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an error state on fetch failure", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(
      makeQueryResult({ isError: true, error: new Error("unreachable") }) as never,
    );
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("unreachable")).toBeInTheDocument();
  });

  it("renders lifecycle, account, and authorization as visually distinct columns", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(makeQueryResult({ data: ROWS }) as never);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("DoubleStraddelAlgo")).toBeInTheDocument();
    expect(screen.getByText("READY")).toBeInTheDocument();
    expect(screen.getByText("READ_ONLY")).toBeInTheDocument();
    expect(screen.getAllByText("INACTIVE").length).toBe(2);
  });

  it("shows STOPPED for a strategy with no assignment, distinctly from READY", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(makeQueryResult({ data: ROWS }) as never);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("STOPPED")).toBeInTheDocument();
    expect(screen.getByText("CombinedVwapNifty")).toBeInTheDocument();
  });

  it("never labels a READY/STOPPED strategy as live-authorized or executing", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(makeQueryResult({ data: ROWS }) as never);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.queryByText(/LIVE_AUTHORIZED/)).not.toBeInTheDocument();
    expect(screen.queryByText("ACTIVE")).not.toBeInTheDocument();
  });

  it("never renders a live-trading or lifecycle-mutation control on this page", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(makeQueryResult({ data: ROWS }) as never);
    renderWithProviders(<TccLifecyclePage />);
    for (const forbidden of [
      /^buy$/i, /^sell$/i, /place order/i, /^start$/i, /^stop$/i, /^pause$/i, /^resume$/i,
      /authorize live/i, /go live/i, /start live trading/i, /execute order/i,
    ]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
  });

  it("distinguishes two strategies on different accounts (cross-account isolation, display-level)", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(makeQueryResult({ data: ROWS }) as never);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("ACC_B")).toBeInTheDocument();
    // CombinedVwapNifty has no account -- rendered as an em dash, never ACC_B's account.
    const rows = screen.getAllByRole("row");
    const vwapRow = rows.find((r) => r.textContent?.includes("CombinedVwapNifty"));
    expect(vwapRow?.textContent).not.toContain("ACC_B");
  });
});
