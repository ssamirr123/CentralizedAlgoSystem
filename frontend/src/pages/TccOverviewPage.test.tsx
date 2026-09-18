import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult } from "@/test/utils";
import { TccOverviewPage } from "./TccOverviewPage";
import * as hooks from "@/api/hooks";

vi.mock("@/api/hooks");

describe("TccOverviewPage (Dashboard)", () => {
  beforeEach(() => vi.resetAllMocks());

  it("renders a loading state while system status or strategies are loading", () => {
    vi.mocked(hooks.useExecutionSystemStatus).mockReturnValue(makeQueryResult({ isLoading: true }) as never);
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ isLoading: true }) as never);
    renderWithProviders(<TccOverviewPage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("renders an error state when system status fails to load", () => {
    vi.mocked(hooks.useExecutionSystemStatus).mockReturnValue(
      makeQueryResult({ isError: true, error: new Error("backend unreachable") }) as never,
    );
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ data: [] }) as never);
    renderWithProviders(<TccOverviewPage />);
    expect(screen.getByText("backend unreachable")).toBeInTheDocument();
  });

  it("renders real system/strategy counts and the kill-switch banner once data loads", () => {
    vi.mocked(hooks.useExecutionSystemStatus).mockReturnValue(
      makeQueryResult({
        data: {
          kill_switch_engaged: false,
          strategy_counts_by_status: { running: 1, shadow: 0, stopped: 2 },
          account_count: 4,
          broker_availability: { angelone: true, dhan: false },
          assigned_strategy_count: 3,
        },
      }) as never,
    );
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(
      makeQueryResult({
        data: [
          { strategy_id: "DoubleStraddelAlgo", status: "shadow", execution_mode: "SHADOW", intents_generated: 0, error_count: 0, last_error: "", started_at: "", stopped_at: "", last_intent_at: "" },
        ],
      }) as never,
    );
    renderWithProviders(<TccOverviewPage />);
    expect(screen.getByText("Execution Overview")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument(); // account_count, unique among these stats
    expect(screen.getByText("3")).toBeInTheDocument(); // assigned_strategy_count, unique
    expect(screen.getByText("DoubleStraddelAlgo")).toBeInTheDocument();
  });

  it("never renders a live-trading control (no BUY/SELL/PLACE ORDER/START/STOP/AUTHORIZE button anywhere)", () => {
    vi.mocked(hooks.useExecutionSystemStatus).mockReturnValue(
      makeQueryResult({
        data: {
          kill_switch_engaged: false,
          strategy_counts_by_status: {},
          account_count: 0,
          broker_availability: {},
          assigned_strategy_count: 0,
        },
      }) as never,
    );
    vi.mocked(hooks.useExecutionStrategies).mockReturnValue(makeQueryResult({ data: [] }) as never);
    renderWithProviders(<TccOverviewPage />);
    for (const forbidden of [/^buy$/i, /^sell$/i, /place order/i, /^start$/i, /^stop$/i, /authorize live/i, /enable live/i]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
  });
});
