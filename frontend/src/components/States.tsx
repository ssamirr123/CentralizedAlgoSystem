import type { ReactNode } from "react";
import { ApiError } from "@/api/client";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state">
      <span className="spinner" /> <span style={{ marginLeft: 8 }}>{label}</span>
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="state">{children}</div>;
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  let msg = "Something went wrong.";
  if (error instanceof ApiError) {
    msg =
      error.status === 401
        ? "Unauthorized — your API key was rejected."
        : error.status === 0
          ? error.detail
          : `${error.status}: ${error.detail}`;
  } else if (error instanceof Error) {
    msg = error.message;
  }
  return (
    <div className="state error">
      <div>{msg}</div>
      {onRetry && (
        <button className="sm ghost" style={{ marginTop: 12 }} onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** Standard render for a react-query result feeding a section. */
export function QueryBoundary<T>({
  query,
  children,
  empty,
}: {
  query: { isLoading: boolean; isError: boolean; error: unknown; data: T | undefined; refetch: () => void };
  children: (data: T) => ReactNode;
  empty?: (data: T) => boolean;
}) {
  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorState error={query.error} onRetry={query.refetch} />;
  if (query.data === undefined) return <EmptyState>No data.</EmptyState>;
  if (empty && empty(query.data)) return <EmptyState>Nothing to show yet.</EmptyState>;
  return <>{children(query.data)}</>;
}
