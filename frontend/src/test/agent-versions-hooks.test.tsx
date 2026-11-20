import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ReactNode } from "react";

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    get: (url: string) => getMock(url),
    // Forwards `body` only when the caller actually passed one -- a rollback
    // calls `api.post(url)` with no second argument at all, and forwarding an
    // explicit `undefined` here would make every assertion below have to
    // account for it, including "posts a rollback with no body".
    post: (url: string, body?: unknown) =>
      body === undefined ? postMock(url) : postMock(url, body),
  },
}));

import {
  agentVersionKeys,
  useAgentDraftStatus,
  useAgentVersionDiff,
  useAgentVersions,
  usePublishAgentVersion,
  useRollbackAgentVersion,
} from "@/lib/agent-versions";

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

describe("agent-version hooks", () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
  });

  it("lists versions from the paged envelope", async () => {
    getMock.mockResolvedValue({
      items: [{ id: "a", versionNo: 2, isCurrent: true, publishedAt: "x" }],
      totalCount: 2,
    });
    const qc = client();
    const { result } = renderHook(() => useAgentVersions("ag-1"), {
      wrapper: wrapper(qc),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/agents/ag-1/versions?limit=50&offset=0");
    expect(result.current.data?.totalCount).toBe(2);
  });

  it("asks for a diff against the working copy when `to` is omitted", async () => {
    getMock.mockResolvedValue({ fromVersionNo: 2, toVersionNo: null, entries: [] });
    const qc = client();
    const { result } = renderHook(() => useAgentVersionDiff("ag-1", 2), {
      wrapper: wrapper(qc),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // No `to` in the query string at all -- sending `to=null` would be parsed
    // as a string by FastAPI and 422.
    expect(getMock).toHaveBeenCalledWith("/agents/ag-1/versions/diff?from=2");
  });

  it("includes `to` when both sides are published versions", async () => {
    getMock.mockResolvedValue({ fromVersionNo: 1, toVersionNo: 3, entries: [] });
    const qc = client();
    const { result } = renderHook(() => useAgentVersionDiff("ag-1", 1, 3), {
      wrapper: wrapper(qc),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/agents/ag-1/versions/diff?from=1&to=3");
  });

  it("does not fetch a diff until a `from` version is chosen", () => {
    const qc = client();
    renderHook(() => useAgentVersionDiff("ag-1", null), { wrapper: wrapper(qc) });
    expect(getMock).not.toHaveBeenCalled();
  });

  it("sends the expected current version number when publishing", async () => {
    postMock.mockResolvedValue({ id: "v2", versionNo: 2, isCurrent: true });
    const qc = client();
    const { result } = renderHook(() => usePublishAgentVersion("ag-1"), {
      wrapper: wrapper(qc),
    });
    await result.current.mutateAsync({ note: "tightened", expectedCurrentVersionNo: 1 });
    expect(postMock).toHaveBeenCalledWith("/agents/ag-1/versions", {
      note: "tightened",
      expectedCurrentVersionNo: 1,
    });
  });

  it("posts a rollback with no body", async () => {
    postMock.mockResolvedValue({ id: "v5", versionNo: 5, rolledBackFrom: 1 });
    const qc = client();
    const { result } = renderHook(() => useRollbackAgentVersion("ag-1"), {
      wrapper: wrapper(qc),
    });
    await result.current.mutateAsync(1);
    expect(postMock).toHaveBeenCalledWith("/agents/ag-1/versions/1/rollback");
  });

  it("invalidates the version list, the draft status and the agent after a publish", async () => {
    postMock.mockResolvedValue({ id: "v2", versionNo: 2 });
    const qc = client();
    const spy = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => usePublishAgentVersion("ag-1"), {
      wrapper: wrapper(qc),
    });
    await result.current.mutateAsync({ expectedCurrentVersionNo: 1 });
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    // The agent itself, because `currentVersionNo` lives on its detail payload,
    // and the header renders it.
    expect(keys).toContain(JSON.stringify(agentVersionKeys.list("ag-1")));
    expect(keys).toContain(JSON.stringify(agentVersionKeys.draftStatus("ag-1")));
    expect(keys).toContain(JSON.stringify(["agents", "ag-1"]));
  });

  it("reads the draft status", async () => {
    getMock.mockResolvedValue({
      dirty: true,
      changedFields: ["mission", "narrowing.odoo"],
      currentVersionNo: 4,
    });
    const qc = client();
    const { result } = renderHook(() => useAgentDraftStatus("ag-1"), {
      wrapper: wrapper(qc),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/agents/ag-1/draft-status");
    expect(result.current.data?.changedFields).toHaveLength(2);
  });
});
