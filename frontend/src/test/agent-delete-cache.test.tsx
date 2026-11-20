import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { useDeleteAgent } from "@/lib/hooks";

// Bug: deleting an agent from its own page refetched the deleted agent's
// queries (["agents", id, ...]) while the page was still mounted. They came
// back 404, the page switched to its error state and unmounted the button
// before its onSuccess could navigate away -- the customer saw an error and a
// second click answered 404.

afterEach(() => vi.restoreAllMocks());

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { qc, wrapper };
}

describe("useDeleteAgent", () => {
  it("drops the deleted agent's own queries instead of refetching them", async () => {
    const { qc, wrapper } = setup();
    qc.setQueryData(["agents", "a1"], { id: "a1" });
    qc.setQueryData(["agents", "a1", "draft-status"], { dirty: false });
    qc.setQueryData(["agents", "a2"], { id: "a2" });
    vi.spyOn(api, "delete").mockResolvedValue({ outcome: "archived" });

    const { result } = renderHook(() => useDeleteAgent(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("a1");
    });

    expect(qc.getQueryCache().find({ queryKey: ["agents", "a1"], exact: true })).toBeUndefined();
    expect(qc.getQueryCache().find({ queryKey: ["agents", "a1", "draft-status"] })).toBeUndefined();
    await waitFor(() => expect(qc.getQueryState(["agents", "a2"])?.isInvalidated).toBe(true));
  });

  it("lets the caller's onSuccess run without waiting for a refetch", async () => {
    const { qc, wrapper } = setup();
    let release: () => void = () => undefined;
    qc.setQueryDefaults(["agents"], {
      queryFn: () => new Promise((resolve) => (release = () => resolve([]))),
    });
    qc.setQueryData(["agents"], []);
    qc.getQueryCache().build(qc, { queryKey: ["agents"] });
    vi.spyOn(api, "delete").mockResolvedValue({ outcome: "deleted" });
    const onSuccess = vi.fn();

    const { result } = renderHook(() => useDeleteAgent(), { wrapper });
    act(() => {
      result.current.mutate("a1", { onSuccess });
    });

    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
    release();
  });
});
