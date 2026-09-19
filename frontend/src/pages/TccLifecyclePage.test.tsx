import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, fireEvent } from "@testing-library/react";
import { renderWithProviders, makeQueryResult, makeMutationResult } from "@/test/utils";
import { TccLifecyclePage } from "./TccLifecyclePage";
import * as hooks from "@/api/hooks";
import * as auth from "@/auth/AuthContext";

vi.mock("@/api/hooks");
vi.mock("@/auth/AuthContext");

const ROWS = [
  {
    strategy_id: "DoubleStraddelAlgo", assignment_id: "DoubleStraddelAlgo", account_id: "ACC_B",
    execution_mode: "SHADOW" as const, strategy_status: "enabled" as const, lifecycle_state: "READY" as const,
    account_authorization_state: "READ_ONLY" as const, live_authorized: false, execution_active: false,
    last_transition_at: "", last_heartbeat_at: "", last_error: "", assignment_exists: true, blocking_reasons: [],
    runtime_state: "INACTIVE" as const, last_cycle_at: "", last_runtime_error: "", last_result_summary: "",
  },
  {
    strategy_id: "CombinedVwapNifty", assignment_id: null, account_id: null,
    execution_mode: "" as const, strategy_status: "disabled" as const, lifecycle_state: "STOPPED" as const,
    account_authorization_state: null, live_authorized: false, execution_active: false,
    last_transition_at: "", last_heartbeat_at: "", last_error: "", assignment_exists: false,
    blocking_reasons: ["no assignment exists for this strategy"],
    runtime_state: "INACTIVE" as const, last_cycle_at: "", last_runtime_error: "", last_result_summary: "",
  },
  {
    strategy_id: "Vwap_Algo_Nifty_hedge", assignment_id: "Vwap_Algo_Nifty_hedge", account_id: "ACC_A",
    execution_mode: "SHADOW" as const, strategy_status: "shadow" as const, lifecycle_state: "RUNNING" as const,
    account_authorization_state: "READ_ONLY" as const, live_authorized: false, execution_active: true,
    last_transition_at: "2026-09-19T00:00:00Z", last_heartbeat_at: "2026-09-19T00:05:00Z", last_error: "",
    assignment_exists: true, runtime_state: "HEALTHY" as const, last_cycle_at: "2026-09-19T00:05:00Z",
    last_runtime_error: "", last_result_summary: "FILLED",
    blocking_reasons: [],
  },
];

function mockAll(canStart: boolean, canStop: boolean, mutate = vi.fn()) {
  vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(makeQueryResult({ data: ROWS }) as never);
  vi.mocked(hooks.useSendStrategyCommand).mockReturnValue(makeMutationResult({ mutate }) as never);
  vi.mocked(auth.useAuth).mockReturnValue({
    hasPermission: (p: string) => (p === "START" ? canStart : p === "STOP" ? canStop : false),
  } as never);
}

describe("TccLifecyclePage", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows a loading state before data arrives", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(makeQueryResult({ isLoading: true }) as never);
    vi.mocked(hooks.useSendStrategyCommand).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an error state on fetch failure", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(
      makeQueryResult({ isError: true, error: new Error("unreachable") }) as never,
    );
    vi.mocked(hooks.useSendStrategyCommand).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("unreachable")).toBeInTheDocument();
  });

  it("labels the page as lifecycle control only, distinct from live trading", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText(/lifecycle control only/i)).toBeInTheDocument();
    expect(screen.getByText(/no order execution/i)).toBeInTheDocument();
  });

  it("renders lifecycle, account, and authorization as visually distinct columns", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("DoubleStraddelAlgo")).toBeInTheDocument();
    expect(screen.getByText("READY")).toBeInTheDocument();
    expect(screen.getAllByText("READ_ONLY").length).toBe(2);
  });

  it("shows Start enabled for a READY (not-yet-running) strategy", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    const rows = screen.getAllByRole("row");
    const readyRow = rows.find((r) => r.textContent?.includes("DoubleStraddelAlgo"))!;
    const startBtn = Array.from(readyRow.querySelectorAll("button")).find((b) => b.textContent === "Start")!;
    expect(startBtn).not.toBeDisabled();
  });

  it("shows Stop disabled for an already-STOPPED strategy", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    const rows = screen.getAllByRole("row");
    const stoppedRow = rows.find((r) => r.textContent?.includes("CombinedVwapNifty"))!;
    const stopBtn = Array.from(stoppedRow.querySelectorAll("button")).find((b) => b.textContent === "Stop")!;
    expect(stopBtn).toBeDisabled();
  });

  it("shows Stop enabled and Start disabled for a RUNNING strategy", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    const rows = screen.getAllByRole("row");
    const runningRow = rows.find((r) => r.textContent?.includes("Vwap_Algo_Nifty_hedge"))!;
    const startBtn = Array.from(runningRow.querySelectorAll("button")).find((b) => b.textContent === "Start")!;
    const stopBtn = Array.from(runningRow.querySelectorAll("button")).find((b) => b.textContent === "Stop")!;
    expect(startBtn).toBeDisabled();
    expect(stopBtn).not.toBeDisabled();
  });

  it("disables both commands for an operator lacking START/STOP permission", () => {
    mockAll(false, false);
    renderWithProviders(<TccLifecyclePage />);
    for (const btn of screen.getAllByRole("button", { name: "Start" })) expect(btn).toBeDisabled();
    for (const btn of screen.getAllByRole("button", { name: "Stop" })) expect(btn).toBeDisabled();
  });

  it("clicking Start invokes the command mutation with exactly START for that strategy", () => {
    const mutate = vi.fn();
    mockAll(true, true, mutate);
    renderWithProviders(<TccLifecyclePage />);
    const rows = screen.getAllByRole("row");
    const readyRow = rows.find((r) => r.textContent?.includes("DoubleStraddelAlgo"))!;
    fireEvent.click(Array.from(readyRow.querySelectorAll("button")).find((b) => b.textContent === "Start")!);
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0][0]).toEqual({ strategyId: "DoubleStraddelAlgo", command: "START" });
  });

  it("displays the command result (including a rejection reason) after a command completes", () => {
    const mutate = vi.fn((_vars, opts) =>
      opts?.onSuccess?.({
        command_id: "c1", strategy_id: "DoubleStraddelAlgo", assignment_id: "DoubleStraddelAlgo",
        account_id: "ACC_B", command: "START", result: "REJECTED", previous_state: "STOPPED",
        new_state: "STOPPED", accepted: false, live_authorized: false, execution_started: false,
        reason: "central kill switch is engaged",
      }),
    );
    mockAll(true, true, mutate);
    renderWithProviders(<TccLifecyclePage />);
    const rows = screen.getAllByRole("row");
    const readyRow = rows.find((r) => r.textContent?.includes("DoubleStraddelAlgo"))!;
    fireEvent.click(Array.from(readyRow.querySelectorAll("button")).find((b) => b.textContent === "Start")!);
    expect(screen.getByText(/START REJECTED/)).toBeInTheDocument();
    expect(screen.getByText(/central kill switch is engaged/)).toBeInTheDocument();
  });

  it("never labels a non-running strategy as executing, and never shows execution_started", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.queryByText(/execution_started/i)).not.toBeInTheDocument();
  });

  it("never renders a live-trading, order, or authorization control on this page", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    for (const forbidden of [
      /^buy$/i, /^sell$/i, /place order/i, /close position/i, /exit position/i,
      /authorize live/i, /go live/i, /start live trading/i, /enable live trading/i, /execute order/i,
    ]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
  });

  it("distinguishes accounts across strategies (cross-account isolation, display-level)", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    const rows = screen.getAllByRole("row");
    const vwapRow = rows.find((r) => r.textContent?.includes("CombinedVwapNifty"))!;
    expect(vwapRow.textContent).not.toContain("ACC_B");
    expect(vwapRow.textContent).not.toContain("ACC_A");
  });

  it("shows the runtime state, execution mode, heartbeat, and last result distinctly per row", () => {
    mockAll(true, true);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("HEALTHY")).toBeInTheDocument();
    expect(screen.getAllByText("INACTIVE").length).toBeGreaterThan(0);
    expect(screen.getAllByText("SHADOW").length).toBe(2);
    expect(screen.getByText("2026-09-19T00:05:00Z")).toBeInTheDocument();
    expect(screen.getByText("FILLED")).toBeInTheDocument();
  });

  it("shows a runtime error distinctly when the runtime has FAILED", () => {
    vi.mocked(hooks.useStrategyLifecycle).mockReturnValue(
      makeQueryResult({
        data: [
          {
            ...ROWS[0], runtime_state: "FAILED" as const,
            last_runtime_error: "resolved broker is not a known-simulated broker",
          },
        ],
      }) as never,
    );
    vi.mocked(hooks.useSendStrategyCommand).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => true } as never);
    renderWithProviders(<TccLifecyclePage />);
    expect(screen.getByText("FAILED")).toBeInTheDocument();
    expect(screen.getByText(/not a known-simulated broker/)).toBeInTheDocument();
  });
});
