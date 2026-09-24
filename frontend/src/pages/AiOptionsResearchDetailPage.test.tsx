import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderRoutedWithProviders } from "@/test/utils";
import { AiOptionsResearchDetailPage } from "./AiOptionsResearchDetailPage";
import * as api from "@/api/aiOptionsResearch";
import type { ResearchResult, ResearchStatus, StagesOut } from "@/api/aiOptionsResearchTypes";

vi.mock("@/api/aiOptionsResearch");

const BASE_STATUS: ResearchStatus = {
  research_id: "r1", underlying: "NIFTY", status: "COMPLETED", current_stage: null,
  created_at: "2026-09-23T00:00:00Z", started_at: "2026-09-23T00:00:01Z", completed_at: "2026-09-23T00:00:10Z",
  error: null, provider_error: null, research_snapshot_id: "snap-1", evidence_quality: "OK",
};

const STAGES: StagesOut = { stages: [], current_stage: null };

beforeEach(() => {
  vi.mocked(api.getOptionsResearchStages).mockResolvedValue(STAGES);
});

describe("AiOptionsResearchDetailPage", () => {
  it("renders a completed report with candidates, never showing an execute control", async () => {
    vi.mocked(api.getOptionsResearchStatus).mockResolvedValue(BASE_STATUS);
    const result: ResearchResult = {
      ...BASE_STATUS,
      report: {
        market_overview: "Range-bound conditions observed.",
        market_regime: { trend: "SIDEWAYS", volatility_regime: "NORMAL", market_structure: "RANGE_BOUND", confidence: "MEDIUM" },
        technical_context: "n/a", news_sentiment: "Not available in this phase",
        options_market_structure: { summary: "Balanced OI." },
        volatility_analysis: { interpretation: "IV is moderate." },
        oi_positioning_analysis: { interpretation: "Roughly balanced positioning." },
        bull_case: { thesis: "Support at OI levels." },
        bear_case: { thesis: "Resistance capping upside." },
        candidates: [{
          strategy: "IRON_CONDOR", supported: true,
          legs: [{ side: "SELL", option_type: "PUT", strike: 24900, price: 60, price_source: "bid", delta: -0.2, iv: 0.13, volume: 1000, open_interest: 5000, bid: 59, ask: 61, liquidity_ok: true, liquidity_reasons: [] }],
          payoff: { net_premium: 70, max_profit: 70, max_profit_unbounded: false, max_loss: 130, max_loss_unbounded: false, breakevens: [24830], pricing_method: "short=bid, long=ask", priced: true, payoff_curve: [] },
          liquidity_ok: true, generation_notes: [], unsupported_reason: null,
          commentary: { fits_reasoning: "fits", invalidation: "breaks below support", market_assumptions: "range-bound", volatility_assumptions: "stable IV", key_levels: [24900] },
          risk_analysis: null,
        }],
        invalidation_conditions: ["A close beyond 25300"],
        final_summary: "AI OPTIONS RESEARCH -- for research purposes only.",
        sanitizer_warnings: [],
      },
    };
    vi.mocked(api.getOptionsResearchResult).mockResolvedValue(result);

    renderRoutedWithProviders(
      [{ path: "/ai-options-research/:researchId", element: <AiOptionsResearchDetailPage /> }],
      { route: "/ai-options-research/r1" },
    );

    await waitFor(() => expect(screen.getByText("IRON CONDOR", { exact: false })).toBeInTheDocument());
    expect(screen.getByText(/AI OPTIONS RESEARCH/)).toBeInTheDocument();
    expect(screen.queryByText(/Execute/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Place Order/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Deploy/i)).not.toBeInTheDocument();
  });

  it("shows a failed state with the error message", async () => {
    vi.mocked(api.getOptionsResearchStatus).mockResolvedValue({ ...BASE_STATUS, status: "FAILED", error: "LLM rate limit reached", provider_error: "LLM_RATE_LIMIT" });
    vi.mocked(api.getOptionsResearchResult).mockResolvedValue({ ...BASE_STATUS, status: "FAILED", report: null });

    renderRoutedWithProviders(
      [{ path: "/ai-options-research/:researchId", element: <AiOptionsResearchDetailPage /> }],
      { route: "/ai-options-research/r1" },
    );

    await waitFor(() => expect(screen.getByText("Research Failed")).toBeInTheDocument());
    expect(screen.getByText("LLM rate limit reached")).toBeInTheDocument();
  });
});
