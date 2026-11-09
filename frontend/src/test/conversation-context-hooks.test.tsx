// frontend/src/test/conversation-context-hooks.test.tsx
// The C3 context hooks against a mocked `api`: the debounced server search,
// the source browser's URL and the pin writer's PUTs.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn());
const put = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { get, post: vi.fn(), put, patch: vi.fn(), delete: vi.fn() },
}));

import {
  conversationKeys,
  useBrowseSource,
  useContextSearchResults,
  useSetPins,
  type ChatSessionDetailDTO,
} from "@/lib/conversation-api";

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const fresh = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });
const searches = () =>
  get.mock.calls.map(([url]) => String(url)).filter((u) => u.startsWith("/chat/context/search"));

/** Let the debounce run out and the query settle. */
async function elapse(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("useContextSearchResults", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    get.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("sends nothing for an empty query without recents, or for null", async () => {
    get.mockResolvedValue([]);
    const { result, rerender } = renderHook(
      ({ q }: { q: string | null }) => useContextSearchResults(q),
      { wrapper: wrapper(fresh()), initialProps: { q: "" as string | null } },
    );
    await elapse(500);
    rerender({ q: "   " });
    await elapse(500);
    rerender({ q: null });
    await elapse(500);
    expect(searches()).toEqual([]);
    expect(result.current).toEqual({ items: [], isLoading: false });
  });

  it("asks once, 200 ms after a burst of typing", async () => {
    get.mockResolvedValue([]);
    const { rerender } = renderHook(({ q }: { q: string }) => useContextSearchResults(q), {
      wrapper: wrapper(fresh()),
      initialProps: { q: "" },
    });
    for (const q of ["p", "pr", "pri", "pric"]) {
      rerender({ q });
      await elapse(50);
    }
    expect(searches()).toEqual([]);
    await elapse(150);
    expect(searches()).toEqual(["/chat/context/search?q=pric"]);
  });

  it("shows recents for an empty query when asked, with the kinds", async () => {
    get.mockResolvedValue([
      { kind: "data_source", id: "d", label: "Drive", hint: null, connected: false },
    ]);
    const { result } = renderHook(
      () => useContextSearchResults("", ["data_source", "knowledge_base"], { recents: true }),
      { wrapper: wrapper(fresh()) },
    );
    expect(result.current.isLoading).toBe(true);
    await elapse(0);
    expect(searches()).toEqual(["/chat/context/search?q=&kinds=data_source%2Cknowledge_base"]);
    expect(result.current).toEqual({
      items: [{ kind: "data_source", id: "d", label: "Drive", hint: undefined, connected: false }],
      isLoading: false,
    });
  });

  it("keeps the previous results while the next ones load", async () => {
    let answer: (hits: unknown) => void = () => undefined;
    get.mockResolvedValueOnce([
      { kind: "agent", id: "a", label: "Ada", hint: null, connected: true },
    ]);
    get.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
    const { result, rerender } = renderHook(({ q }: { q: string }) => useContextSearchResults(q), {
      wrapper: wrapper(fresh()),
      initialProps: { q: "a" },
    });
    await elapse(0);
    expect(result.current.items.map((i) => i.label)).toEqual(["Ada"]);
    rerender({ q: "ad" });
    await elapse(250);
    expect(searches()).toHaveLength(2);
    // Still loading "ad": "a"'s hits stay, and no skeleton replaces them.
    expect(result.current).toMatchObject({ isLoading: false, items: [{ label: "Ada" }] });
    await act(async () => {
      answer([]);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.items).toEqual([]);
  });
});

describe("useBrowseSource", () => {
  beforeEach(() => {
    get.mockReset();
  });

  it("encodes the path and asks only for a uuid source", async () => {
    get.mockResolvedValue({ entries: [], truncated: false });
    const id = "6f1c1f0e-8f9a-4a39-9d3e-2f4f6b1d2c3a";
    renderHook(() => useBrowseSource("not-a-uuid", "/"), { wrapper: wrapper(fresh()) });
    const { result } = renderHook(() => useBrowseSource(id, "/a b/c&d?e"), {
      wrapper: wrapper(fresh()),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(get.mock.calls.map(([u]) => u)).toEqual([
      `/chat/data-sources/${id}/browse?path=%2Fa%20b%2Fc%26d%3Fe`,
    ]);
  });
});

describe("useSetPins", () => {
  const pin = (id: string) => ({ kind: "knowledge_base", id, label: id, available: true });
  const session = (pins: string[]) =>
    ({ id: "s/1", pinnedRefs: pins.map(pin) }) as unknown as ChatSessionDetailDTO;

  beforeEach(() => {
    put.mockReset();
  });

  it("encodes the session id", async () => {
    const qc = fresh();
    qc.setQueryData(conversationKeys.session("s/1"), session(["a"]));
    put.mockResolvedValue(session(["a", "b"]));
    const { result } = renderHook(() => useSetPins("s/1"), { wrapper: wrapper(qc) });
    await act(() => result.current.mutateAsync({ add: { kind: "knowledge_base", id: "b" } }));
    expect(put).toHaveBeenCalledWith("/chat/sessions/s%2F1/pins", {
      refs: [
        { kind: "knowledge_base", id: "a" },
        { kind: "knowledge_base", id: "b" },
      ],
    });
  });

  it("two quick removals end with both pins removed", async () => {
    const qc = fresh();
    qc.setQueryData(conversationKeys.session("s/1"), session(["a", "b", "c"]));
    // The server answers with what it was sent.
    put.mockImplementation(async (_url: string, body: { refs: { id: string }[] }) =>
      session(body.refs.map((r) => r.id)),
    );
    const { result } = renderHook(() => useSetPins("s/1"), { wrapper: wrapper(qc) });
    act(() => {
      result.current.mutate({ remove: { kind: "knowledge_base", id: "a" } });
      result.current.mutate({ remove: { kind: "knowledge_base", id: "b" } });
    });
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(
        qc
          .getQueryData<ChatSessionDetailDTO>(conversationKeys.session("s/1"))
          ?.pinnedRefs.map((p) => p.id),
      ).toEqual(["c"]),
    );
    expect(put.mock.calls[1][1]).toEqual({ refs: [{ kind: "knowledge_base", id: "c" }] });
  });
});
