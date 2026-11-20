import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

// The costs page shows this month by default, can switch to all time, and
// says how much of the input came from the prompt cache.

const get = vi.fn((_path: string) => Promise.resolve([]));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: (path: string) => get(path) },
}));

import { useUsage } from "@/lib/hooks";
import { InputTokensMetric, PeriodSwitch } from "@/routes/costs";

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

describe("useUsage", () => {
  it("asks for this month by default", async () => {
    renderHook(() => useUsage("agent"), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/usage?group_by=agent&period=month"));
  });

  it("asks for all time when told to", async () => {
    renderHook(() => useUsage("department", "all"), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/usage?group_by=department&period=all"));
  });
});

describe("PeriodSwitch", () => {
  it("marks the current period and reports a switch", () => {
    const onChange = vi.fn();
    render(<PeriodSwitch period="month" onChange={onChange} />);
    expect(screen.getByRole("button", { name: "This month" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "All time" }));
    expect(onChange).toHaveBeenCalledWith("all");
  });
});

describe("InputTokensMetric", () => {
  it("says how much of the input came from the cache", () => {
    render(<InputTokensMetric tokensIn={2_000_000} cacheReadTokens={1_500_000} />);
    expect(screen.getByText(/of which from cache: 1\.50M/)).toBeInTheDocument();
  });

  it("says nothing about the cache when none was read", () => {
    render(<InputTokensMetric tokensIn={2_000_000} cacheReadTokens={0} />);
    expect(screen.queryByText(/from cache/)).not.toBeInTheDocument();
  });
});
