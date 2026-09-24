import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderRoutedWithProviders } from "@/test/utils";
import { AiResearchDetailPage } from "./AiResearchDetailPage";
import * as api from "@/api/aiResearch";
import type { ResearchResult, ResearchStatus, Stages } from "@/api/aiResearchTypes";

vi.mock("@/api/aiResearch");

function route() {
  return [{ path: "/ai-research/:researchId", element: <AiResearchDetailPage /> }];
}

const BASE_STATUS: Omit<ResearchStatus, "status" | "stage" | "current_stage" | "error" | "provider_error"> = {
  research_id: "r1", symbol: "AAPL", research_date: "2026-09-22", research_depth: "standard",
  created_at: "2026-09-22T00:00:00Z", started_at: "2026-09-22T00:01:00Z", completed_at: null,
  market: "US", exchange: "GENERIC", currency: "USD", market_data_provenance: null,
};

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("AiResearchDetailPage", () => {
  it("shows QUEUED state", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "QUEUED", stage: "QUEUED", current_stage: null, error: null, provider_error: null,
      started_at: null,
    });
    vi.mocked(api.getResearchStages).mockResolvedValue({
      research_id: "r1", status: "QUEUED", current_stage: null, stages: [],
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });
    await waitFor(() => expect(screen.getByText("QUEUED")).toBeInTheDocument());
  });

  it("shows RUNNING state with real returned stages only (selected analysts)", async () => {
    const stages: Stages = {
      research_id: "r1", status: "RUNNING", current_stage: "News Analyst",
      stages: [
        { name: "Market Analyst", status: "COMPLETED", started_at: null, completed_at: null, duration_ms: 4200 },
        { name: "News Analyst", status: "RUNNING", started_at: null, completed_at: null, duration_ms: null },
      ],
    };
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "RUNNING", stage: "ANALYSTS", current_stage: "News Analyst",
      error: null, provider_error: null,
    });
    vi.mocked(api.getResearchStages).mockResolvedValue(stages);
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });

    await waitFor(() => expect(screen.getByText("Market Analyst")).toBeInTheDocument());
    // "News Analyst" legitimately appears twice (the stage-list entry AND
    // the "current stage" summary line) -- assert presence, not uniqueness.
    expect(screen.getAllByText("News Analyst").length).toBeGreaterThan(0);
    // Only two real analysts were selected for this run -- the other two
    // must never appear as fabricated PENDING entries.
    expect(screen.queryByText("Fundamentals Analyst")).not.toBeInTheDocument();
    expect(screen.queryByText("Sentiment Analyst")).not.toBeInTheDocument();
  });

  it("labels the inferred current stage honestly, never as a fake percentage", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "RUNNING", stage: "ANALYSTS", current_stage: "Market Analyst",
      error: null, provider_error: null,
    });
    vi.mocked(api.getResearchStages).mockResolvedValue({
      research_id: "r1", status: "RUNNING", current_stage: "Market Analyst",
      stages: [{ name: "Market Analyst", status: "RUNNING", started_at: null, completed_at: null, duration_ms: null }],
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });
    await waitFor(() => expect(screen.getByText(/inferred/i)).toBeInTheDocument());
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("shows COMPLETED result with Trader, Risk, and Portfolio Manager output, plus the research-only warning", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", current_stage: "Portfolio Manager",
      completed_at: "2026-09-22T00:10:00Z", error: null, provider_error: null,
    });
    const result: ResearchResult = {
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", completed_at: "2026-09-22T00:10:00Z",
      error: null, provider_error: null,
      report: {
        market_analysis: "Uptrend", news_analysis: null, sentiment_analysis: null, fundamentals_analysis: null,
        bull_case: "Strong earnings", bear_case: "Valuation stretched", research_manager_decision: "Buy",
        trader_plan: "Accumulate on dips", risk_aggressive: "Go long", risk_conservative: "Wait",
        risk_neutral: "Small position", risk_judge_decision: "Moderate long", final_trade_decision: "Hold overall",
        signal: "Hold",
      },
    };
    vi.mocked(api.getResearch).mockResolvedValue(result);
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });

    await waitFor(() => expect(screen.getAllByText(/AI Research Only/i).length).toBeGreaterThan(0));

    const user = await import("@testing-library/user-event");
    await user.default.setup({ delay: null }).click(screen.getByRole("tab", { name: "Trader" }));
    expect(screen.getByText("Accumulate on dips")).toBeInTheDocument();
    expect(screen.getByText("AI RESEARCH DECISION", { exact: false })).toBeInTheDocument();

    await user.default.setup({ delay: null }).click(screen.getByRole("tab", { name: "Risk" }));
    expect(screen.getByText("Go long")).toBeInTheDocument();
    expect(screen.getByText("Moderate long")).toBeInTheDocument();

    await user.default.setup({ delay: null }).click(screen.getByRole("tab", { name: "Portfolio Manager" }));
    expect(screen.getByText("Hold overall")).toBeInTheDocument();
  });

  it("shows the Market Data provenance panel for a completed India research result", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", current_stage: "Portfolio Manager",
      completed_at: "2026-09-22T00:10:00Z", error: null, provider_error: null,
      market: "INDIA", exchange: "NSE", currency: "INR",
    });
    const result: ResearchResult = {
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", completed_at: "2026-09-22T00:10:00Z",
      error: null, provider_error: null, market: "INDIA", exchange: "NSE", currency: "INR",
      market_data_provenance: {
        status: "AVAILABLE", source: "ICICI_BREEZE", provider: "icici_breeze", symbol: "NIFTY",
        interval: "5minute", from: "2026-01-17", to: "2026-01-27", cutoff: "2026-01-27T10:00:00Z",
        candle_count: 42,
      },
      report: {
        market_analysis: "Uptrend", news_analysis: null, sentiment_analysis: null, fundamentals_analysis: null,
        bull_case: null, bear_case: null, research_manager_decision: null, trader_plan: null,
        risk_aggressive: null, risk_conservative: null, risk_neutral: null, risk_judge_decision: null,
        final_trade_decision: "Hold", signal: "Hold",
      },
    };
    vi.mocked(api.getResearch).mockResolvedValue(result);
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });

    await waitFor(() => expect(screen.getByText("Market Data")).toBeInTheDocument());
    expect(screen.getByText("ICICI Breeze (live fetch)")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("shows no Market Data panel for US research", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", current_stage: "Portfolio Manager",
      completed_at: "2026-09-22T00:10:00Z", error: null, provider_error: null,
    });
    vi.mocked(api.getResearch).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", completed_at: "2026-09-22T00:10:00Z",
      error: null, provider_error: null, market_data_provenance: null,
      report: {
        market_analysis: null, news_analysis: null, sentiment_analysis: null, fundamentals_analysis: null,
        bull_case: null, bear_case: null, research_manager_decision: null, trader_plan: null,
        risk_aggressive: null, risk_conservative: null, risk_neutral: null, risk_judge_decision: null,
        final_trade_decision: "Hold", signal: "Hold",
      },
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });
    await waitFor(() => expect(screen.getAllByText(/AI Research Only/i).length).toBeGreaterThan(0));
    expect(screen.queryByText("Market Data")).not.toBeInTheDocument();
  });

  it("shows FAILED state with sanitized error, provider rate-limit message, and completed stages preserved", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "FAILED", stage: "FAILED", current_stage: "News Analyst",
      completed_at: "2026-09-22T00:05:00Z",
      error: "TradingAgents research failed: Error code: 429 ...", provider_error: "RATE_LIMIT",
    });
    vi.mocked(api.getResearchStages).mockResolvedValue({
      research_id: "r1", status: "FAILED", current_stage: "News Analyst",
      stages: [
        { name: "Market Analyst", status: "COMPLETED", started_at: null, completed_at: null, duration_ms: 3000 },
        { name: "News Analyst", status: "FAILED", started_at: null, completed_at: null, duration_ms: null },
      ],
    });
    vi.mocked(api.getResearch).mockResolvedValue({
      ...BASE_STATUS, status: "FAILED", stage: "FAILED", completed_at: "2026-09-22T00:05:00Z",
      error: "TradingAgents research failed: Error code: 429 ...", provider_error: "RATE_LIMIT", report: null,
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });

    await waitFor(() => expect(screen.getByText(/rate limit reached/i)).toBeInTheDocument());
    expect(screen.getByText("Market Analyst")).toBeInTheDocument(); // completed stage preserved
    expect(screen.getByRole("button", { name: /resume research/i })).toBeInTheDocument();
  });

  it("resume shows honest wording when there was no checkpoint to continue from", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "FAILED", stage: "FAILED", current_stage: "News Analyst",
      completed_at: "2026-09-22T00:05:00Z", error: "simulated", provider_error: null,
    });
    vi.mocked(api.getResearchStages).mockResolvedValue({
      research_id: "r1", status: "FAILED", current_stage: null, stages: [],
    });
    vi.mocked(api.getResearch).mockResolvedValue({
      ...BASE_STATUS, status: "FAILED", stage: "FAILED", completed_at: "2026-09-22T00:05:00Z",
      error: "simulated", provider_error: null, report: null,
    });
    vi.mocked(api.resumeResearch).mockResolvedValue({
      original_research_id: "r1", new_research_id: "r2", status: "QUEUED", resumed_from_checkpoint: false,
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });

    const user = (await import("@testing-library/user-event")).default.setup({ delay: null });
    await waitFor(() => expect(screen.getByRole("button", { name: /resume research/i })).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /resume research/i }));

    await waitFor(() =>
      expect(screen.getByText(/could not continue exactly where it stopped/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/continued exactly where it stopped/i)).not.toBeInTheDocument();
  });

  it("stops polling status once COMPLETED", async () => {
    vi.useRealTimers(); // avoid fake-timer/waitFor interaction; this test waits in real time instead
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", current_stage: "Portfolio Manager",
      completed_at: "2026-09-22T00:10:00Z", error: null, provider_error: null,
    });
    vi.mocked(api.getResearch).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", completed_at: "2026-09-22T00:10:00Z",
      error: null, provider_error: null, report: null,
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });
    await waitFor(() => expect(screen.getByText("COMPLETED")).toBeInTheDocument());
    const countAfterMount = vi.mocked(api.getResearchStatus).mock.calls.length;

    // POLL_MS is 2000ms -- if polling didn't stop, waiting past it would
    // grow the call count further. It must not, regardless of how many
    // calls React's own mount/effect cycle produced.
    await new Promise((r) => setTimeout(r, 2500));
    expect(vi.mocked(api.getResearchStatus).mock.calls.length).toBe(countAfterMount);
  }, 10000);

  it("shows a clear disabled state rather than crashing", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({ enabled: false, message: "AI Research is disabled" });
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });
    await waitFor(() => expect(screen.getByText(/currently disabled/i)).toBeInTheDocument());
  });

  it("never renders an execution control", async () => {
    vi.mocked(api.getResearchStatus).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", current_stage: "Portfolio Manager",
      completed_at: "2026-09-22T00:10:00Z", error: null, provider_error: null,
    });
    vi.mocked(api.getResearch).mockResolvedValue({
      ...BASE_STATUS, status: "COMPLETED", stage: "COMPLETED", completed_at: "2026-09-22T00:10:00Z",
      error: null, provider_error: null,
      report: {
        market_analysis: null, news_analysis: null, sentiment_analysis: null, fundamentals_analysis: null,
        bull_case: null, bear_case: null, research_manager_decision: null, trader_plan: "BUY",
        risk_aggressive: null, risk_conservative: null, risk_neutral: null, risk_judge_decision: null,
        final_trade_decision: "Buy", signal: "Buy",
      },
    });
    renderRoutedWithProviders(route(), { route: "/ai-research/r1" });
    await waitFor(() => expect(screen.getAllByText(/AI Research Only/i).length).toBeGreaterThan(0));
    for (const forbidden of [/place order/i, /execute trade/i, /^buy now$/i, /^sell now$/i, /start strategy/i]) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    }
  });
});
