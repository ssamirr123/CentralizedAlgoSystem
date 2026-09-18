import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult, makeMutationResult } from "@/test/utils";
import { TccAssignmentsPage } from "./TccAssignmentsPage";
import * as hooks from "@/api/hooks";
import * as auth from "@/auth/AuthContext";

vi.mock("@/api/hooks");
vi.mock("@/auth/AuthContext");

const ASSIGNMENTS = [
  { strategy_id: "DoubleStraddelAlgo", account_id: "ACC_B", execution_mode: "PAPER" as const, risk_profile: "default", enabled: true },
];

function mockCommon() {
  vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ data: [] }) as never);
  vi.mocked(hooks.useExecutionAccounts).mockReturnValue(makeQueryResult({ data: [] }) as never);
  vi.mocked(hooks.useExecutionModes).mockReturnValue(makeQueryResult({ data: [] }) as never);
  vi.mocked(hooks.useCreateExecutionAssignment).mockReturnValue(makeMutationResult({}) as never);
}

describe("TccAssignmentsPage", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows UNASSIGNED (an explicit empty state) when no assignment exists yet -- never fabricates one", () => {
    mockCommon();
    vi.mocked(hooks.useExecutionAssignments).mockReturnValue(makeQueryResult({ data: [] }) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccAssignmentsPage />);
    expect(screen.getByText("Nothing to show yet.")).toBeInTheDocument();
  });

  it("renders a real, existing assignment (Strategy -> Account -> mode)", () => {
    mockCommon();
    vi.mocked(hooks.useExecutionAssignments).mockReturnValue(makeQueryResult({ data: ASSIGNMENTS }) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccAssignmentsPage />);
    expect(screen.getByText("DoubleStraddelAlgo")).toBeInTheDocument();
    expect(screen.getByText("ACC_B")).toBeInTheDocument();
  });

  it("disables the create-assignment controls for an operator lacking TRADING_CONTROL", () => {
    mockCommon();
    vi.mocked(hooks.useExecutionAssignments).mockReturnValue(makeQueryResult({ data: [] }) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => false } as never);
    renderWithProviders(<TccAssignmentsPage />);
    expect(screen.getByRole("button", { name: "Create assignment" })).toBeDisabled();
  });

  it("never renders a live-trading control on this page", () => {
    mockCommon();
    vi.mocked(hooks.useExecutionAssignments).mockReturnValue(makeQueryResult({ data: [] }) as never);
    vi.mocked(auth.useAuth).mockReturnValue({ hasPermission: () => true } as never);
    renderWithProviders(<TccAssignmentsPage />);
    for (const forbidden of [/^buy$/i, /^sell$/i, /place order/i, /authorize live/i, /enable live/i]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
  });
});
