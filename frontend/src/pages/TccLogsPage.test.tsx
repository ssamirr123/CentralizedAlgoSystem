import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult } from "@/test/utils";
import { TccLogsPage } from "./TccLogsPage";
import * as hooks from "@/api/hooks";

vi.mock("@/api/hooks");

const AUDIT_ENTRIES = [
  { id: 1, timestamp: "2026-09-18T10:00:00Z", actor: "user:1", actor_label: "samir", action: "KILL_SWITCH_ENGAGED", target: null, outcome: "success", ip: null, detail: null },
];

describe("TccLogsPage (Audit)", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows an explicit empty state, not a blank page, when there are no audit entries yet", () => {
    vi.mocked(hooks.useExecutionAuditLog).mockReturnValue(makeQueryResult({ data: [] }) as never);
    renderWithProviders(<TccLogsPage />);
    expect(screen.getByText("Nothing to show yet.")).toBeInTheDocument();
  });

  it("shows an error state on fetch failure", () => {
    vi.mocked(hooks.useExecutionAuditLog).mockReturnValue(
      makeQueryResult({ isError: true, error: new Error("audit store unavailable") }) as never,
    );
    renderWithProviders(<TccLogsPage />);
    expect(screen.getByText("audit store unavailable")).toBeInTheDocument();
  });

  it("renders a real audit entry's actor/action/outcome (correlation surfaced via actor/action/target columns)", () => {
    vi.mocked(hooks.useExecutionAuditLog).mockReturnValue(makeQueryResult({ data: AUDIT_ENTRIES }) as never);
    renderWithProviders(<TccLogsPage />);
    expect(screen.getByText("samir")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "KILL_SWITCH_ENGAGED" })).toBeInTheDocument();
    expect(screen.getByText("SUCCESS")).toBeInTheDocument();
  });

  it("never renders a delete/modify control -- the audit trail is presented strictly append-only/read-only", () => {
    vi.mocked(hooks.useExecutionAuditLog).mockReturnValue(makeQueryResult({ data: AUDIT_ENTRIES }) as never);
    renderWithProviders(<TccLogsPage />);
    for (const forbidden of [/delete/i, /^edit$/i, /modify/i, /clear/i]) {
      expect(screen.queryByRole("button", { name: forbidden })).not.toBeInTheDocument();
    }
    // The one legitimate button on this page is a read-only refetch.
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });
});
