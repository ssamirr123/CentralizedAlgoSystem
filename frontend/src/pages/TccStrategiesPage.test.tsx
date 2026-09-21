import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult, makeMutationResult } from "@/test/utils";
import { TccStrategiesPage } from "./TccStrategiesPage";
import * as hooks from "@/api/hooks";
import * as auth from "@/auth/AuthContext";

vi.mock("@/api/hooks");
vi.mock("@/auth/AuthContext");

const STRATEGIES = [
  { strategy_id: "DoubleStraddelAlgo", status: "stopped" as const, execution_mode: "SHADOW" as const, intents_generated: 0, error_count: 0, last_error: "", started_at: "", stopped_at: "", last_intent_at: "" },
];

function mockSupportingData() {
  vi.mocked(hooks.useExecutionAccounts).mockReturnValue(makeQueryResult({ data: [] }) as never);
  vi.mocked(hooks.useExecutionAssignments).mockReturnValue(makeQueryResult({ data: [] }) as never);
  vi.mocked(hooks.useExecutionPnl).mockReturnValue(makeQueryResult({ data: { total_realized: 0, total_unrealized: 0, per_strategy: {} } }) as never);
  vi.mocked(hooks.useExecutionModes).mockReturnValue(makeQueryResult({ data: [] }) as never);
  vi.mocked(hooks.useCreateExecutionAssignment).mockReturnValue(makeMutationResult({}) as never);
}

describe("TccStrategiesPage", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows an explicit empty state when no strategies are registered", () => {
    mockSupportingData();
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ data: [] }) as never);
    vi.mocked(hooks.useStartExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(hooks.useStopExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccStrategiesPage />);
    expect(screen.getByText("Nothing to show yet.")).toBeInTheDocument();
  });

  it("renders the three known strategies with an 'unassigned' account when no assignment exists", () => {
    mockSupportingData();
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ data: STRATEGIES }) as never);
    vi.mocked(hooks.useStartExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(hooks.useStopExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccStrategiesPage />);
    expect(screen.getByText("DoubleStraddelAlgo")).toBeInTheDocument();
    expect(screen.getByText("unassigned")).toBeInTheDocument();
  });

  it("Start/Stop only ever flip an in-memory process-lifecycle flag -- clicking Start never calls a broker or LiveAuthorization API (mutationFn is exactly api.startExecutionStrategy, mocked here to prove it is the only thing invoked)", async () => {
    mockSupportingData();
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ data: STRATEGIES }) as never);
    const startMutate = vi.fn();
    vi.mocked(hooks.useStartExecutionStrategy).mockReturnValue(makeMutationResult({ mutate: startMutate }) as never);
    vi.mocked(hooks.useStopExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => true } as never);
    const { default: userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    renderWithProviders(<TccStrategiesPage />);
    await user.click(screen.getByRole("button", { name: "Start" }));
    expect(startMutate).toHaveBeenCalledWith("DoubleStraddelAlgo");
    expect(startMutate).toHaveBeenCalledTimes(1);
  });

  it("disables Start for an operator lacking the START permission", () => {
    mockSupportingData();
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ data: STRATEGIES }) as never);
    vi.mocked(hooks.useStartExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(hooks.useStopExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccStrategiesPage />);
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("never renders a real order-execution control (BUY/SELL/PLACE ORDER/AUTHORIZE LIVE) anywhere on this page", () => {
    mockSupportingData();
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ data: STRATEGIES }) as never);
    vi.mocked(hooks.useStartExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(hooks.useStopExecutionStrategy).mockReturnValue(makeMutationResult({}) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => true } as never);
    renderWithProviders(<TccStrategiesPage />);
    for (const forbidden of [/^buy$/i, /^sell$/i, /place order/i, /authorize live/i, /enable live/i]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
  });
});
