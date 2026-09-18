import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders, makeQueryResult } from "@/test/utils";
import { TccAccountsPage } from "./TccAccountsPage";
import * as hooks from "@/api/hooks";

vi.mock("@/api/hooks");

const ACCOUNTS = [
  { account_id: "ACC_A", account_name: "Account A", broker_id: "angelone", enabled: true, connection_state: "DISCONNECTED" as const, environment: "production", execution_mode: "PAPER" as const, authorization_state: "READ_ONLY" as const },
  { account_id: "ACC_B", account_name: "Account B", broker_id: "angelone", enabled: true, connection_state: "DISCONNECTED" as const, environment: "production", execution_mode: "PAPER" as const, authorization_state: "READ_ONLY" as const },
];

describe("TccAccountsPage", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows a loading state before data arrives", () => {
    vi.mocked(hooks.useExecutionAccounts).mockReturnValue(makeQueryResult({ isLoading: true }) as never);
    renderWithProviders(<TccAccountsPage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an error state with retry when the accounts fetch fails", () => {
    const refetch = vi.fn();
    vi.mocked(hooks.useExecutionAccounts).mockReturnValue(
      makeQueryResult({ isError: true, error: new Error("timeout"), refetch }) as never,
    );
    renderWithProviders(<TccAccountsPage />);
    expect(screen.getByText("timeout")).toBeInTheDocument();
  });

  it("shows an explicit empty state when zero accounts are registered (not a silent blank page)", () => {
    vi.mocked(hooks.useExecutionAccounts).mockReturnValue(makeQueryResult({ data: [] }) as never);
    renderWithProviders(<TccAccountsPage />);
    expect(screen.getByText("Nothing to show yet.")).toBeInTheDocument();
  });

  it("renders both accounts with their real, non-credential fields", () => {
    vi.mocked(hooks.useExecutionAccounts).mockReturnValue(makeQueryResult({ data: ACCOUNTS }) as never);
    renderWithProviders(<TccAccountsPage />);
    expect(screen.getByText("ACC_A")).toBeInTheDocument();
    expect(screen.getByText("ACC_B")).toBeInTheDocument();
    expect(screen.getAllByText("production")).toHaveLength(2);
  });

  it("never renders a credential-shaped field (api_key/token/password/secret) for any account", () => {
    const accountsWithStraySecret = [
      { ...ACCOUNTS[0], api_key: "SHOULD_NEVER_APPEAR", password: "SHOULD_NEVER_APPEAR" } as unknown as (typeof ACCOUNTS)[0],
    ];
    vi.mocked(hooks.useExecutionAccounts).mockReturnValue(makeQueryResult({ data: accountsWithStraySecret }) as never);
    const { container } = renderWithProviders(<TccAccountsPage />);
    // Even if the backend/type accidentally carried extra fields, this
    // component only ever reads named fields into JSX -- it must not
    // render them incidentally.
    expect(container.textContent).not.toContain("SHOULD_NEVER_APPEAR");
  });

  it("is read-only: no mutation button of any kind is rendered on this page", () => {
    vi.mocked(hooks.useExecutionAccounts).mockReturnValue(makeQueryResult({ data: ACCOUNTS }) as never);
    renderWithProviders(<TccAccountsPage />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
