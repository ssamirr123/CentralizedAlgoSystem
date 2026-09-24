import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import { renderWithProviders } from "@/test/utils";
import { AiResearchPage } from "./AiResearchPage";
import * as api from "@/api/aiResearch";
import * as backtestApi from "@/api/aiResearchBacktest";
import type { ResearchConfigOptions, ResearchHistoryEntry, MemoryEntry } from "@/api/aiResearchTypes";

vi.mock("@/api/aiResearch");
vi.mock("@/api/aiResearchBacktest");

const CONFIG: ResearchConfigOptions = {
  enabled: true,
  analysts: ["market", "social", "news", "fundamentals"],
  llm_providers: ["openai", "groq", "anthropic"],
  research_depths: ["quick", "standard", "deep"],
  debate_rounds_range: [1, 5],
  risk_debate_rounds_range: [1, 5],
  retry_count_range: [0, 10],
  data_vendors: { core_stock_apis: ["yfinance", "alpha_vantage"], macro_data: ["fred"] },
  max_concurrent: 1,
  timeout_seconds: 300,
};

beforeEach(() => {
  vi.mocked(api.getConfigOptions).mockResolvedValue(CONFIG);
  vi.mocked(api.getResearchHistory).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 25 });
  vi.mocked(api.getDecisionMemory).mockResolvedValue([]);
  vi.mocked(backtestApi.getBacktestHistory).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
  vi.mocked(backtestApi.estimateBacktest).mockResolvedValue({
    total_runs: 4, symbol_count: 1, date_count: 4, dates: [], exceeds_limit: false, limit_message: null,
    limits: { max_symbols: 3, max_dates: 12, max_runs: 20, max_concurrent: 1, default_holding_days: 5 },
  });
});

describe("AiResearchPage", () => {
  it("renders and loads configuration", async () => {
    renderWithProviders(<AiResearchPage />);
    expect(screen.getByText("AI Research Engine")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Technical")).toBeInTheDocument());
  });

  it("shows a clear disabled state, not a generic crash", async () => {
    vi.mocked(api.getConfigOptions).mockResolvedValue({ ...CONFIG, enabled: false });
    renderWithProviders(<AiResearchPage />);
    await waitFor(() => expect(screen.getByText(/currently disabled/i)).toBeInTheDocument());
    expect(screen.getByText(/AI_RESEARCH_ENABLED/)).toBeInTheDocument();
  });

  it("lets the user toggle analyst selection", async () => {
    renderWithProviders(<AiResearchPage />);
    const technical = (await screen.findByLabelText("Technical")) as HTMLInputElement;
    expect(technical.checked).toBe(true);
    fireEvent.click(technical);
    expect(technical.checked).toBe(false);
  });

  it("disables submit when no analysts are selected", async () => {
    renderWithProviders(<AiResearchPage />);
    for (const label of ["Technical", "Fundamentals", "News", "Sentiment"]) {
      fireEvent.click(await screen.findByLabelText(label));
    }
    expect(screen.getByRole("button", { name: /run ai research/i })).toBeDisabled();
  });

  it("submits research and does not hold the request open", async () => {
    vi.mocked(api.createResearch).mockResolvedValue({
      research_id: "abc-123", status: "QUEUED", created_at: "2026-09-22T00:00:00Z",
    });
    renderWithProviders(<AiResearchPage />);
    await screen.findByLabelText("Technical");
    fireEvent.click(screen.getByRole("button", { name: /run ai research/i }));
    await waitFor(() => expect(api.createResearch).toHaveBeenCalledTimes(1));
    const body = vi.mocked(api.createResearch).mock.calls[0][0];
    expect(body.symbol).toBe("AAPL");
    expect(body.selected_analysts).toEqual(["market", "fundamentals", "news", "social"]);
  });

  it("shows research history and notes durable, database-backed storage", async () => {
    const history: ResearchHistoryEntry[] = [
      { research_id: "r1", symbol: "AAPL", research_date: "2026-09-22", research_depth: "standard",
        status: "COMPLETED", llm_provider: "groq", created_at: "2026-09-22T00:00:00Z", completed_at: "2026-09-22T00:05:00Z",
        market: "US", currency: "USD" },
    ];
    vi.mocked(api.getResearchHistory).mockResolvedValue({ items: history, total: 1, page: 1, page_size: 25 });
    renderWithProviders(<AiResearchPage />);
    fireEvent.click(screen.getByRole("tab", { name: "Research History" }));
    await waitFor(() => expect(screen.getByText("AAPL")).toBeInTheDocument());
    expect(screen.getByText(/survives a backend restart/i)).toBeInTheDocument();
  });

  it("shows empty history state without a blank screen", async () => {
    renderWithProviders(<AiResearchPage />);
    fireEvent.click(screen.getByRole("tab", { name: "Research History" }));
    await waitFor(() => expect(screen.getByText(/nothing to show/i)).toBeInTheDocument());
  });

  it("shows decision memory entries", async () => {
    const entries: MemoryEntry[] = [
      { memory_id: "2026-09-20:NVDA", ticker: "NVDA", trade_date: "2026-09-20", rating: "Hold",
        pending: true, decision: "Rating: Hold", reflection: null },
    ];
    vi.mocked(api.getDecisionMemory).mockResolvedValue(entries);
    renderWithProviders(<AiResearchPage />);
    fireEvent.click(screen.getByRole("tab", { name: "Decision Memory" }));
    await waitFor(() => expect(screen.getByText("NVDA")).toBeInTheDocument());
  });

  it("shows empty memory state without a blank screen", async () => {
    renderWithProviders(<AiResearchPage />);
    fireEvent.click(screen.getByRole("tab", { name: "Decision Memory" }));
    await waitFor(() => expect(screen.getByText(/nothing to show/i)).toBeInTheDocument());
  });

  it("shows the Backtesting tab with a setup form and run-count estimate", async () => {
    renderWithProviders(<AiResearchPage />);
    fireEvent.click(screen.getByRole("tab", { name: "Backtesting" }));
    await waitFor(() => expect(screen.getByText("New Backtest")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText(/Estimated research runs/)).toBeInTheDocument());
  });

  it("shows backtest history and lets the user navigate to a past backtest", async () => {
    vi.mocked(backtestApi.getBacktestHistory).mockResolvedValue({
      items: [{
        backtest_id: "bt-1", symbols: ["AAPL"], start_date: "2026-01-01", end_date: "2026-01-22",
        frequency: "weekly", market: "US", status: "COMPLETED", total_runs: 4, completed_runs: 4, failed_runs: 0,
        created_at: "2026-09-22T00:00:00Z", completed_at: "2026-09-22T00:10:00Z",
      }],
      total: 1, page: 1, page_size: 20,
    });
    renderWithProviders(<AiResearchPage />);
    fireEvent.click(screen.getByRole("tab", { name: "Backtesting" }));
    fireEvent.click(screen.getByRole("tab", { name: "Backtest History" }));
    await waitFor(() => expect(screen.getByText("AAPL")).toBeInTheDocument());
  });

  it("never renders an execution control anywhere on the page", async () => {
    renderWithProviders(<AiResearchPage />);
    await screen.findByLabelText("Technical");
    for (const forbidden of [/place order/i, /execute trade/i, /^buy now$/i, /^sell now$/i, /start strategy/i]) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    }
  });
});
