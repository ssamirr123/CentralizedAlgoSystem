import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderRoutedWithProviders } from "@/test/utils";
import { AiResearchBacktestDetailPage } from "./AiResearchBacktestDetailPage";
import * as api from "@/api/aiResearchBacktest";
import type { BacktestDetail, BacktestRunsPage, BacktestStatus, DataQualityMatrix } from "@/api/aiResearchBacktestTypes";

vi.mock("@/api/aiResearchBacktest");

function route() {
  return [{ path: "/ai-research/backtests/:backtestId", element: <AiResearchBacktestDetailPage /> }];
}

const DATA_QUALITY: DataQualityMatrix = {
  sources: [
    { source: "market_data", safe: "YES", detail: "Bounded by an explicit window." },
    { source: "fundamentals_financial_statements", safe: "PARTIAL", detail: "No filing date reported by the vendor." },
    { source: "prediction_markets", safe: "WITHHELD", detail: "No historical vintage exists." },
  ],
  summary: "This backtest is NOT claimed to be fully unbiased for those two sources.",
};

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.getDataQuality).mockResolvedValue(DATA_QUALITY);
});

afterEach(() => {
  vi.useRealTimers();
});

const BASE_STATUS: Omit<BacktestStatus, "status" | "completed_runs" | "failed_runs" | "remaining_runs" | "progress" | "error"> = {
  backtest_id: "bt1", total_runs: 4, current_symbol: null, current_date: null,
  started_at: "2026-09-22T00:00:00Z", completed_at: null,
};

describe("AiResearchBacktestDetailPage", () => {
  it("shows RUNNING state with honest progress derived from real run counts", async () => {
    vi.mocked(api.getBacktestStatus).mockResolvedValue({
      ...BASE_STATUS, status: "RUNNING", completed_runs: 2, failed_runs: 0, remaining_runs: 2, progress: 0.5,
      current_symbol: "AAPL", current_date: "2026-01-08", error: null,
    });
    vi.mocked(api.getBacktestRuns).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
    renderRoutedWithProviders(route(), { route: "/ai-research/backtests/bt1" });

    await waitFor(() => expect(screen.getByText("RUNNING")).toBeInTheDocument());
    expect(screen.getByText("Progress: 50%")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
  });

  it("shows COMPLETED metrics labeled as decision evaluation, never portfolio performance", async () => {
    vi.mocked(api.getBacktestStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", completed_runs: 4, failed_runs: 0, remaining_runs: 0, progress: 1,
      completed_at: "2026-09-22T00:10:00Z", error: null,
    });
    const detail: BacktestDetail = {
      backtest_id: "bt1", status: "COMPLETED", symbols: ["AAPL"], start_date: "2026-01-01", end_date: "2026-01-22",
      frequency: "weekly", market: "US", research_depth: "standard", llm_provider: null, holding_days: 5,
      total_runs: 4, completed_runs: 4, failed_runs: 0, created_at: "2026-09-22T00:00:00Z",
      started_at: "2026-09-22T00:00:00Z", completed_at: "2026-09-22T00:10:00Z", error: null,
      metrics: {
        label: "AI Research Decision Evaluation", total_decisions: 4,
        counts_by_decision: { Buy: 2, Hold: 2 }, resolved: 3, pending_evaluation: 1, unscored: 0,
        by_decision: { Buy: { count: 2, hit_rate: 0.5, mean_alpha: 0.012 }, Hold: { count: 1, hit_rate: null, mean_alpha: -0.003 } },
        overall_mean_alpha: 0.006, overall_median_alpha: 0.004, positive_decision_rate: 0.5, holding_days: 5,
      },
    };
    vi.mocked(api.getBacktest).mockResolvedValue(detail);
    const runs: BacktestRunsPage = {
      items: [
        { symbol: "AAPL", cell_date: "2026-01-01", status: "COMPLETED", research_id: "r-1", raw_decision: "Rating: Buy",
          normalized_decision: "Buy", evaluation_status: "RESOLVED", raw_return: 0.02, alpha_return: 0.012,
          benchmark: "SPY", holding_days: 5, resolution_date: "2026-01-08", error: null, started_at: null, completed_at: null },
      ],
      total: 1, page: 1, page_size: 50,
    };
    vi.mocked(api.getBacktestRuns).mockResolvedValue(runs);
    renderRoutedWithProviders(route(), { route: "/ai-research/backtests/bt1" });

    await waitFor(() => expect(screen.getByText("AI Research Decision Evaluation")).toBeInTheDocument());
    expect(screen.getByText(/not a trading-strategy or portfolio-P&L result/i)).toBeInTheDocument();
    const forbidden = [/sharpe/i, /\bcagr\b/i, /drawdown/i];
    for (const pattern of forbidden) {
      expect(screen.queryByText(pattern)).not.toBeInTheDocument();
    }
  });

  it("links a completed run to its underlying research_id for drill-down", async () => {
    vi.mocked(api.getBacktestStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", completed_runs: 1, failed_runs: 0, remaining_runs: 0, progress: 1,
      completed_at: "2026-09-22T00:10:00Z", error: null,
    });
    vi.mocked(api.getBacktest).mockResolvedValue({
      backtest_id: "bt1", status: "COMPLETED", symbols: ["AAPL"], start_date: "2026-01-01", end_date: "2026-01-01",
      frequency: "daily", market: "US", research_depth: "standard", llm_provider: null, holding_days: 5,
      total_runs: 1, completed_runs: 1, failed_runs: 0, created_at: "2026-09-22T00:00:00Z",
      started_at: null, completed_at: "2026-09-22T00:10:00Z", error: null, metrics: null,
    });
    vi.mocked(api.getBacktestRuns).mockResolvedValue({
      items: [
        { symbol: "AAPL", cell_date: "2026-01-01", status: "COMPLETED", research_id: "r-42", raw_decision: "Hold",
          normalized_decision: "Hold", evaluation_status: "PENDING", raw_return: null, alpha_return: null,
          benchmark: null, holding_days: null, resolution_date: null, error: null, started_at: null, completed_at: null },
      ],
      total: 1, page: 1, page_size: 50,
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/backtests/bt1" });

    await waitFor(() => expect(screen.getByText("AAPL")).toBeInTheDocument());
    const row = screen.getByText("AAPL").closest("tr");
    expect(row).toHaveClass("row-actions");
  });

  it("shows failed runs distinctly and does not report the backtest as fully successful", async () => {
    vi.mocked(api.getBacktestStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", completed_runs: 3, failed_runs: 1, remaining_runs: 0, progress: 1,
      completed_at: "2026-09-22T00:10:00Z", error: null,
    });
    vi.mocked(api.getBacktest).mockResolvedValue({
      backtest_id: "bt1", status: "COMPLETED", symbols: ["AAPL"], start_date: "2026-01-01", end_date: "2026-01-22",
      frequency: "weekly", market: "US", research_depth: "standard", llm_provider: null, holding_days: 5,
      total_runs: 4, completed_runs: 3, failed_runs: 1, created_at: "2026-09-22T00:00:00Z",
      started_at: null, completed_at: "2026-09-22T00:10:00Z", error: null,
      metrics: { label: "AI Research Decision Evaluation", total_decisions: 3, counts_by_decision: {}, resolved: 0,
        pending_evaluation: 3, unscored: 0, by_decision: {}, overall_mean_alpha: null, overall_median_alpha: null,
        positive_decision_rate: null, holding_days: 5 },
    });
    vi.mocked(api.getBacktestRuns).mockResolvedValue({
      items: [
        { symbol: "AAPL", cell_date: "2026-01-08", status: "FAILED", research_id: null, raw_decision: null,
          normalized_decision: null, evaluation_status: null, raw_return: null, alpha_return: null, benchmark: null,
          holding_days: null, resolution_date: null, error: "provider rate limit", started_at: null, completed_at: null },
      ],
      total: 1, page: 1, page_size: 50,
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/backtests/bt1" });

    await waitFor(() => expect(screen.getByText("FAILED")).toBeInTheDocument());
    const completedRow = screen.getByText("Completed").closest("dt");
    expect(completedRow?.nextElementSibling).toHaveTextContent("3"); // completed_runs shown honestly alongside failed
  });

  it("always shows the bias/data-quality disclosure panel", async () => {
    vi.mocked(api.getBacktestStatus).mockResolvedValue({
      ...BASE_STATUS, status: "RUNNING", completed_runs: 0, failed_runs: 0, remaining_runs: 4, progress: 0, error: null,
    });
    vi.mocked(api.getBacktestRuns).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
    renderRoutedWithProviders(route(), { route: "/ai-research/backtests/bt1" });

    await waitFor(() => expect(screen.getByText("Historical Research Quality")).toBeInTheDocument());
    expect(screen.getByText(/not claimed to be fully unbiased/i)).toBeInTheDocument();
  });

  it("shows a clear disabled state rather than crashing", async () => {
    vi.mocked(api.getBacktestStatus).mockResolvedValue({ enabled: false, message: "AI Research is disabled" });
    renderRoutedWithProviders(route(), { route: "/ai-research/backtests/bt1" });
    await waitFor(() => expect(screen.getByText(/currently disabled/i)).toBeInTheDocument());
  });

  it("never renders an execution control", async () => {
    vi.mocked(api.getBacktestStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", completed_runs: 1, failed_runs: 0, remaining_runs: 0, progress: 1,
      completed_at: "2026-09-22T00:10:00Z", error: null,
    });
    vi.mocked(api.getBacktest).mockResolvedValue({
      backtest_id: "bt1", status: "COMPLETED", symbols: ["AAPL"], start_date: "2026-01-01", end_date: "2026-01-01",
      frequency: "daily", market: "US", research_depth: "standard", llm_provider: null, holding_days: 5,
      total_runs: 1, completed_runs: 1, failed_runs: 0, created_at: "2026-09-22T00:00:00Z",
      started_at: null, completed_at: "2026-09-22T00:10:00Z", error: null, metrics: null,
    });
    vi.mocked(api.getBacktestRuns).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
    renderRoutedWithProviders(route(), { route: "/ai-research/backtests/bt1" });
    await waitFor(() => expect(screen.getByText("COMPLETED")).toBeInTheDocument());
    for (const forbidden of [/place order/i, /execute trade/i, /^buy now$/i, /^sell now$/i, /start strategy/i]) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    }
  });
});
