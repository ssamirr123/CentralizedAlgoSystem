import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import { renderWithProviders } from "@/test/utils";
import { BacktestSetupForm } from "./BacktestSetupForm";
import * as researchApi from "@/api/aiResearch";
import * as backtestApi from "@/api/aiResearchBacktest";
import type { ResearchConfigOptions } from "@/api/aiResearchTypes";
import type { BacktestEstimate } from "@/api/aiResearchBacktestTypes";

vi.mock("@/api/aiResearch");
vi.mock("@/api/aiResearchBacktest");

const CONFIG: ResearchConfigOptions = {
  enabled: true, analysts: ["market", "social", "news", "fundamentals"],
  llm_providers: ["openai", "groq"], research_depths: ["quick", "standard", "deep"],
  debate_rounds_range: [1, 5], risk_debate_rounds_range: [1, 5], retry_count_range: [0, 10],
  data_vendors: { core_stock_apis: ["yfinance"] }, max_concurrent: 1, timeout_seconds: 300,
};

const ESTIMATE: BacktestEstimate = {
  total_runs: 4, symbol_count: 1, date_count: 4, dates: ["2026-01-01", "2026-01-08", "2026-01-15", "2026-01-22"],
  exceeds_limit: false, limit_message: null,
  limits: { max_symbols: 3, max_dates: 12, max_runs: 20, max_concurrent: 1, default_holding_days: 5 },
};

beforeEach(() => {
  vi.mocked(researchApi.getConfigOptions).mockResolvedValue(CONFIG);
  vi.mocked(backtestApi.estimateBacktest).mockResolvedValue(ESTIMATE);
});

describe("BacktestSetupForm", () => {
  it("shows the estimated run count before submission", async () => {
    renderWithProviders(<BacktestSetupForm onSubmitted={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Estimated research runs: 4/)).toBeInTheDocument());
  });

  it("blocks submission and explains why when the estimate exceeds a configured limit", async () => {
    vi.mocked(backtestApi.estimateBacktest).mockResolvedValue({
      ...ESTIMATE, total_runs: 50, exceeds_limit: true,
      limit_message: "50 research runs exceeds the configured limit of 20 (AI_BACKTEST_MAX_RUNS)",
    });
    renderWithProviders(<BacktestSetupForm onSubmitted={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/cannot run/i)).toBeInTheDocument());
    expect(screen.getByText(/AI_BACKTEST_MAX_RUNS/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /run backtest/i })).toBeDisabled();
  });

  it("disables submit when no analysts are selected", async () => {
    renderWithProviders(<BacktestSetupForm onSubmitted={vi.fn()} />);
    for (const label of ["Technical", "Fundamentals", "News", "Sentiment"]) {
      fireEvent.click(await screen.findByLabelText(label));
    }
    expect(screen.getByRole("button", { name: /run backtest/i })).toBeDisabled();
  });

  it("submits and returns the new backtest_id", async () => {
    vi.mocked(backtestApi.submitBacktest).mockResolvedValue({
      backtest_id: "bt-new", status: "QUEUED", total_runs: 4, created_at: "2026-09-22T00:00:00Z",
    });
    const onSubmitted = vi.fn();
    renderWithProviders(<BacktestSetupForm onSubmitted={onSubmitted} />);
    await waitFor(() => expect(screen.getByText(/Estimated research runs/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /run backtest/i }));
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith("bt-new"));
  });

  it("shows a clear disabled state rather than crashing", async () => {
    vi.mocked(researchApi.getConfigOptions).mockResolvedValue({ ...CONFIG, enabled: false });
    renderWithProviders(<BacktestSetupForm onSubmitted={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/currently disabled/i)).toBeInTheDocument());
  });
});
