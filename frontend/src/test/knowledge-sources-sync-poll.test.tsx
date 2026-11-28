// A sync started anywhere -- the wizard, "Add source", another tab, before
// the page was reloaded -- used to show "queued" until the page was reloaded,
// because only the one Sync button that started it ever polled the job. The
// source list now carries each source's newest job and keeps asking while one
// is queued or running, and refreshes the knowledge bases when it settles.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const { getSources } = vi.hoisted(() => ({ getSources: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: {
    get: (path: string) =>
      path.startsWith("/knowledge/sources")
        ? getSources()
        : Promise.reject(new Error(`unexpected GET ${path}`)),
  },
}));

import { useDataSources } from "@/lib/hooks";
import { syncInFlight } from "@/lib/sync-status";

function page(status: string | null) {
  return {
    items: [{ id: "s1", name: "site", latestJobId: status ? "j1" : null, latestJobStatus: status }],
    totalCount: 1,
  };
}

describe("useDataSources", () => {
  it("keeps asking while a sync is in flight and refreshes the bases once it settles", async () => {
    getSources
      .mockResolvedValueOnce(page("queued"))
      .mockResolvedValueOnce(page("running"))
      .mockResolvedValue(page("succeeded"));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useDataSources({ pageSize: 200 }, { pollMs: 10 }), {
      wrapper,
    });

    await waitFor(() => expect(result.current.data?.items[0].latestJobStatus).toBe("succeeded"));
    expect(getSources.mock.calls.length).toBeGreaterThanOrEqual(3);
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["knowledge", "bases"] }),
    );
    const calls = getSources.mock.calls.length;
    await new Promise((r) => setTimeout(r, 50));
    expect(getSources.mock.calls.length).toBe(calls);
  });

  it("names a queued or running job as in flight, and nothing else", () => {
    expect(syncInFlight({ latestJobStatus: "queued" })).toBe(true);
    expect(syncInFlight({ latestJobStatus: "running" })).toBe(true);
    expect(syncInFlight({ latestJobStatus: "partial" })).toBe(false);
    expect(syncInFlight({ latestJobStatus: null })).toBe(false);
    expect(syncInFlight({})).toBe(false);
  });
});
