import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useRuns } from "@/lib/hooks";
import { api } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: vi.fn() } };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
});

const runs = [{ id: "r1", agentId: "a1", state: "running", steps: 2, toolCalls: [] }];

describe("useRuns", () => {
  it("asks for ten runs by default", async () => {
    vi.mocked(api.get).mockResolvedValue(runs);
    const { result } = renderHook(() => useRuns(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api.get).toHaveBeenCalledWith("/runs?limit=10");
    expect(result.current.data).toEqual(runs);
  });

  it("passes a state filter and a limit through", async () => {
    vi.mocked(api.get).mockResolvedValue(runs);
    const { result } = renderHook(
      () => useRuns({ state: "waiting_for_approval,waiting_for_input", limit: 5 }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = vi.mocked(api.get).mock.calls[0][0] as string;
    expect(url).toContain("state=waiting_for_approval%2Cwaiting_for_input");
    expect(url).toContain("limit=5");
  });

  it("keys the two filters apart so one does not serve the other's cache", async () => {
    vi.mocked(api.get).mockResolvedValue(runs);
    const { result: all } = renderHook(() => useRuns(), { wrapper });
    const { result: parked } = renderHook(() => useRuns({ state: "waiting_for_approval" }), {
      wrapper,
    });
    await waitFor(() => expect(all.current.isSuccess).toBe(true));
    await waitFor(() => expect(parked.current.isSuccess).toBe(true));
    expect(vi.mocked(api.get).mock.calls.length).toBe(2);
  });
});
