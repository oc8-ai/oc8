import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { useBoard, useBoards, useDeleteBoard } from "@/lib/boards-api";

vi.mock("@/lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  retryUnlessRefused: () => false,
}));

describe("useDeleteBoard", () => {
  it("drops the deleted board's queries and refreshes only the lists", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path.startsWith("/boards/") ? { id: path.slice(8) } : [],
    );
    vi.mocked(api.delete).mockResolvedValue(undefined);

    // Mounted: both lists and the board being deleted (as an open tile is).
    renderHook(() => useBoards(), { wrapper });
    renderHook(() => useBoards("agent-1"), { wrapper });
    const tile = renderHook(() => useBoard("b1"), { wrapper });
    await waitFor(() => expect(tile.result.current.isSuccess).toBe(true));
    expect(api.get).toHaveBeenCalledTimes(3);
    qc.setQueryData(["boards", "b1", "query", { dataset: "sales" }], { series: [] });
    qc.setQueryData(["boards", "b2"], { id: "b2" });

    const { result } = renderHook(() => useDeleteBoard(), { wrapper });
    result.current.mutate({ id: "b1", records: 4 });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(api.delete).toHaveBeenCalledWith("/boards/b1?records=4");

    // The deleted board and its block queries are gone, not refetched.
    expect(qc.getQueryData(["boards", "b1"])).toBeUndefined();
    expect(qc.getQueryCache().findAll({ queryKey: ["boards", "b1", "query"] })).toHaveLength(0);
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(5));
    const paths = vi
      .mocked(api.get)
      .mock.calls.slice(3)
      .map(([p]) => p);
    expect(paths.sort()).toEqual(["/boards", "/boards?agentId=agent-1"]);
    await new Promise((r) => setTimeout(r, 20));
    expect(api.get).toHaveBeenCalledTimes(5);
    expect(vi.mocked(api.get).mock.calls.filter(([p]) => p === "/boards/b1")).toHaveLength(1);
    // Another board is left alone.
    expect(qc.getQueryState(["boards", "b2"])?.isInvalidated).toBe(false);
  });

  it("refreshes the board's count and the lists when records arrived meanwhile", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    vi.mocked(api.get).mockReset();
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path.startsWith("/boards/") ? { id: path.slice(8), recordCount: 4 } : [],
    );
    const refused = Object.assign(new Error("board_records_changed"), { status: 409 });
    vi.mocked(api.delete).mockRejectedValue(refused);

    renderHook(() => useBoards(), { wrapper });
    const tile = renderHook(() => useBoard("b1"), { wrapper });
    await waitFor(() => expect(tile.result.current.isSuccess).toBe(true));
    qc.setQueryData(["boards", "b2"], { id: "b2" });
    const before = vi.mocked(api.get).mock.calls.length;

    const { result } = renderHook(() => useDeleteBoard(), { wrapper });
    result.current.mutate({ id: "b1", records: 4 });
    await waitFor(() => expect(result.current.isError).toBe(true));

    // Both read again: the board's detail and the list; nothing removed.
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(before + 2));
    const paths = vi
      .mocked(api.get)
      .mock.calls.slice(before)
      .map(([p]) => p);
    expect(paths.sort()).toEqual(["/boards", "/boards/b1"]);
    expect(qc.getQueryState(["boards", "b2"])?.isInvalidated).toBe(false);
    expect(qc.getQueryData(["boardDeleted", "b1"])).toBeUndefined();
  });

  it("leaves the queries alone on any other refusal", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    qc.setQueryData(["boards", "b1"], { id: "b1" });
    vi.mocked(api.delete).mockRejectedValue(Object.assign(new Error("nope"), { status: 403 }));
    const { result } = renderHook(() => useDeleteBoard(), { wrapper });
    result.current.mutate({ id: "b1", records: 4 });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(qc.getQueryState(["boards", "b1"])?.isInvalidated).toBe(false);
  });
});
