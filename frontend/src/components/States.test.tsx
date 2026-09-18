import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Loading, EmptyState, ErrorState, QueryBoundary } from "./States";
import { ApiError } from "@/api/client";

describe("Loading", () => {
  it("renders a loading indicator with the default label", () => {
    render(<Loading />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("renders a custom label", () => {
    render(<Loading label="Fetching accounts…" />);
    expect(screen.getByText("Fetching accounts…")).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("renders its children", () => {
    render(<EmptyState>Nothing to show yet.</EmptyState>);
    expect(screen.getByText("Nothing to show yet.")).toBeInTheDocument();
  });
});

describe("ErrorState", () => {
  it("shows a generic message for a plain Error", () => {
    render(<ErrorState error={new Error("boom")} />);
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("shows a specific 401 message for an ApiError", () => {
    render(<ErrorState error={new ApiError(401, "unauthorized")} />);
    expect(screen.getByText(/Unauthorized/)).toBeInTheDocument();
  });

  it("shows status + detail for other ApiError statuses", () => {
    render(<ErrorState error={new ApiError(500, "internal error")} />);
    expect(screen.getByText("500: internal error")).toBeInTheDocument();
  });

  it("calls onRetry when the Retry button is clicked", () => {
    const onRetry = vi.fn();
    render(<ErrorState error={new Error("boom")} onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders no Retry button when onRetry is not supplied", () => {
    render(<ErrorState error={new Error("boom")} />);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });
});

describe("QueryBoundary", () => {
  it("renders Loading while isLoading is true", () => {
    render(
      <QueryBoundary query={{ isLoading: true, isError: false, error: null, data: undefined, refetch: () => {} }}>
        {() => <div>should not render</div>}
      </QueryBoundary>,
    );
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("renders ErrorState when isError is true, even if data is present", () => {
    render(
      <QueryBoundary
        query={{ isLoading: false, isError: true, error: new Error("network down"), data: [1, 2, 3], refetch: () => {} }}
      >
        {() => <div>should not render</div>}
      </QueryBoundary>,
    );
    expect(screen.getByText("network down")).toBeInTheDocument();
  });

  it("renders an explicit empty/unavailable state when data is undefined (not loading, not error)", () => {
    render(
      <QueryBoundary query={{ isLoading: false, isError: false, error: null, data: undefined, refetch: () => {} }}>
        {() => <div>should not render</div>}
      </QueryBoundary>,
    );
    expect(screen.getByText("No data.")).toBeInTheDocument();
  });

  it("renders the empty-state message when `empty(data)` returns true", () => {
    render(
      <QueryBoundary
        query={{ isLoading: false, isError: false, error: null, data: [] as number[], refetch: () => {} }}
        empty={(d) => d.length === 0}
      >
        {() => <div>should not render</div>}
      </QueryBoundary>,
    );
    expect(screen.getByText("Nothing to show yet.")).toBeInTheDocument();
  });

  it("renders children(data) once data is present and non-empty", () => {
    render(
      <QueryBoundary
        query={{ isLoading: false, isError: false, error: null, data: ["a", "b"], refetch: () => {} }}
        empty={(d) => d.length === 0}
      >
        {(data) => <div>{data.join(",")}</div>}
      </QueryBoundary>,
    );
    expect(screen.getByText("a,b")).toBeInTheDocument();
  });

  it("never calls children while loading or erroring (read-only rendering discipline)", () => {
    const children = vi.fn(() => <div>rendered</div>);
    render(
      <QueryBoundary query={{ isLoading: true, isError: false, error: null, data: undefined, refetch: () => {} }}>
        {children}
      </QueryBoundary>,
    );
    expect(children).not.toHaveBeenCalled();
  });
});
