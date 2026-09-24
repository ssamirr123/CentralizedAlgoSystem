import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import { renderWithProviders } from "@/test/utils";
import { ResearchSetupForm } from "./ResearchSetupForm";
import * as researchApi from "@/api/aiResearch";
import * as marketApi from "@/api/market";
import type { ResearchConfigOptions } from "@/api/aiResearchTypes";
import type { CalendarResponse, InstrumentSearchResponse } from "@/api/marketTypes";

vi.mock("@/api/aiResearch");
vi.mock("@/api/market");

const CONFIG: ResearchConfigOptions = {
  enabled: true, analysts: ["market", "social", "news", "fundamentals"],
  llm_providers: ["openai", "groq"], research_depths: ["quick", "standard", "deep"],
  debate_rounds_range: [1, 5], risk_debate_rounds_range: [1, 5], retry_count_range: [0, 10],
  data_vendors: { core_stock_apis: ["yfinance"] }, max_concurrent: 1, timeout_seconds: 300,
};

const NIFTY_SEARCH: InstrumentSearchResponse = {
  items: [
    { canonical_id: "NIFTY_50", market: "INDIA", exchange: "NSE", instrument_type: "INDEX", symbol: "NIFTY 50", display_name: "NIFTY 50", currency: "INR", timezone: "Asia/Kolkata" },
    { canonical_id: "NIFTY_BANK", market: "INDIA", exchange: "NSE", instrument_type: "INDEX", symbol: "NIFTY BANK", display_name: "NIFTY Bank (Bank Nifty)", currency: "INR", timezone: "Asia/Kolkata" },
  ],
};

const VALID_CALENDAR: CalendarResponse = {
  market: "INDIA", timezone: "Asia/Kolkata", covered_years: [2025, 2026],
  validation: { requested: "2026-09-22", is_valid: true, reason: null, previous_trading_day: null, next_trading_day: null },
};

beforeEach(() => {
  vi.mocked(researchApi.getConfigOptions).mockResolvedValue(CONFIG);
  vi.mocked(marketApi.searchInstruments).mockResolvedValue(NIFTY_SEARCH);
  vi.mocked(marketApi.getCalendar).mockResolvedValue(VALID_CALENDAR);
});

describe("ResearchSetupForm market selector", () => {
  it("defaults to US market", async () => {
    renderWithProviders(<ResearchSetupForm onSubmitted={vi.fn()} />);
    await screen.findByLabelText("Technical");
    expect(screen.getByLabelText("Currency")).toHaveValue("USD");
  });

  it("switching to India shows instrument search and INR currency", async () => {
    renderWithProviders(<ResearchSetupForm onSubmitted={vi.fn()} />);
    await screen.findByLabelText("Technical");
    fireEvent.change(screen.getByLabelText("Market"), { target: { value: "INDIA" } });

    expect(screen.getByLabelText("Currency")).toHaveValue("INR");
    expect(screen.getByLabelText("Instrument")).toBeInTheDocument();
  });

  it("selecting NIFTY 50 from instrument search sets the symbol", async () => {
    renderWithProviders(<ResearchSetupForm onSubmitted={vi.fn()} />);
    await screen.findByLabelText("Technical");
    fireEvent.change(screen.getByLabelText("Market"), { target: { value: "INDIA" } });
    fireEvent.change(screen.getByLabelText("Instrument"), { target: { value: "nifty" } });

    await waitFor(() => expect(screen.getByText(/NIFTY 50 \(NIFTY 50\)/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/NIFTY 50 \(NIFTY 50\)/));

    expect(screen.getByText("Selected: NIFTY 50")).toBeInTheDocument();
  });

  it("shows a non-trading-date warning with previous/next trading day, never silently changing the date", async () => {
    vi.mocked(marketApi.getCalendar).mockResolvedValue({
      market: "INDIA", timezone: "Asia/Kolkata", covered_years: [2025, 2026],
      validation: {
        requested: "2026-01-26", is_valid: false, reason: "2026-01-26 is not an NSE trading day (Republic Day)",
        previous_trading_day: "2026-01-23", next_trading_day: "2026-01-27",
      },
    });
    renderWithProviders(<ResearchSetupForm onSubmitted={vi.fn()} />);
    await screen.findByLabelText("Technical");
    fireEvent.change(screen.getByLabelText("Market"), { target: { value: "INDIA" } });
    fireEvent.change(screen.getByLabelText("Research date"), { target: { value: "2026-01-26" } });

    await waitFor(() => expect(screen.getByText(/Republic Day/)).toBeInTheDocument());
    expect(screen.getByText(/2026-01-23/)).toBeInTheDocument();
    expect(screen.getByText(/2026-01-27/)).toBeInTheDocument();
    // The date field itself must still show exactly what the user picked.
    expect(screen.getByLabelText("Research date")).toHaveValue("2026-01-26");
  });

  it("submits with market=INDIA in the request body", async () => {
    vi.mocked(researchApi.createResearch).mockResolvedValue({
      research_id: "r-india", status: "QUEUED", created_at: "2026-09-22T00:00:00Z",
    });
    renderWithProviders(<ResearchSetupForm onSubmitted={vi.fn()} />);
    await screen.findByLabelText("Technical");
    fireEvent.change(screen.getByLabelText("Market"), { target: { value: "INDIA" } });
    fireEvent.click(screen.getByRole("button", { name: /run ai research/i }));

    await waitFor(() => expect(researchApi.createResearch).toHaveBeenCalled());
    const body = vi.mocked(researchApi.createResearch).mock.calls.at(-1)![0];
    expect(body.market).toBe("INDIA");
    expect(body.symbol).toBe("NIFTY");
  });

  it("US submission omits India-only UI and keeps existing regression behavior", async () => {
    vi.mocked(researchApi.createResearch).mockResolvedValue({
      research_id: "r-us", status: "QUEUED", created_at: "2026-09-22T00:00:00Z",
    });
    renderWithProviders(<ResearchSetupForm onSubmitted={vi.fn()} />);
    await screen.findByLabelText("Technical");
    expect(screen.queryByLabelText("Instrument")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /run ai research/i }));

    await waitFor(() => expect(researchApi.createResearch).toHaveBeenCalled());
    const body = vi.mocked(researchApi.createResearch).mock.calls.at(-1)![0];
    expect(body.market).toBe("US");
    expect(body.symbol).toBe("AAPL");
  });
});
