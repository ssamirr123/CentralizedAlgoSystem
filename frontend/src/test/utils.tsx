import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

/** Fresh QueryClient per render -- no cross-test cache leakage, no retries
 * (a failing mocked query must resolve to an error immediately, not retry
 * and time the test out). */
export function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A minimal react-query UseQueryResult stand-in, enough for QueryBoundary
 * and every page's own isLoading/isError branches. Not the real hook --
 * pages under test have their `use*` hook imports mocked via vi.mock to
 * return this shape directly, so no real HTTP call is ever made. */
export function makeQueryResult<T>(overrides: {
  data?: T;
  isLoading?: boolean;
  isError?: boolean;
  error?: unknown;
  refetch?: () => void;
}) {
  return {
    data: overrides.data,
    isLoading: overrides.isLoading ?? false,
    isError: overrides.isError ?? false,
    error: overrides.error ?? null,
    refetch: overrides.refetch ?? (() => {}),
  };
}

export function makeMutationResult(overrides: {
  mutate?: (...args: unknown[]) => void;
  isPending?: boolean;
  isSuccess?: boolean;
  error?: unknown;
  reset?: () => void;
}) {
  return {
    mutate: overrides.mutate ?? (() => {}),
    isPending: overrides.isPending ?? false,
    isSuccess: overrides.isSuccess ?? false,
    error: overrides.error ?? null,
    reset: overrides.reset ?? (() => {}),
  };
}
