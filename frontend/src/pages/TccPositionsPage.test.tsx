import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult } from "@/test/utils";
import { TccPositionsPage } from "./TccPositionsPage";
import * as hooks from "@/api/hooks";

vi.mock("@/api/hooks");

describe("TccPositionsPage", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows a loading state before data arrives", () => {
    vi.mocked(hooks.useExecutionPositions).mockReturnValue(makeQueryResult({ isLoading: true }) as never);
    renderWithProviders(<TccPositionsPage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an error state on fetch failure", () => {
    vi.mocked(hooks.useExecutionPositions).mockReturnValue(
      makeQueryResult({ isError: true, error: new Error("down") }) as never,
    );
    renderWithProviders(<TccPositionsPage />);
    expect(screen.getByText("down")).toBeInTheDocument();
  });

  it("honestly reports zero positions rather than fabricating any (Phase 10's real, current scope)", () => {
    vi.mocked(hooks.useExecutionPositions).mockReturnValue(makeQueryResult({ data: [] }) as never);
    renderWithProviders(<TccPositionsPage />);
    expect(screen.getByText(/No positions yet/)).toBeInTheDocument();
  });

  it("renders a real position row with the normalized fields", () => {
    vi.mocked(hooks.useExecutionPositions).mockReturnValue(
      makeQueryResult({
        data: [{ strategy_id: "S1", account_id: "ACC_B", symbol: "NIFTY", quantity: 65, average_price: 27.4, last_price: 28.1, pnl: 45.5 }],
      }) as never,
    );
    renderWithProviders(<TccPositionsPage />);
    expect(screen.getByText("S1")).toBeInTheDocument();
    expect(screen.getByText("NIFTY")).toBeInTheDocument();
    expect(screen.getByText("65")).toBeInTheDocument();
  });

  it("is read-only: no mutation button (no place/modify/cancel order control) is rendered", () => {
    vi.mocked(hooks.useExecutionPositions).mockReturnValue(makeQueryResult({ data: [] }) as never);
    renderWithProviders(<TccPositionsPage />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
