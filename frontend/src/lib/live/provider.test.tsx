import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.hoisted: `vi.mock` factories are hoisted above ordinary top-level
// `const`s by vitest -- see the identical pattern/comment in
// `src/test/profile.test.tsx`.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getToken: vi.fn().mockResolvedValue("fake-token"),
  };
});

import { LiveUpdatesProvider, useLiveConnectionStatus, useWatchRuns } from "@/lib/live/provider";

// Same minimal fake as `connection.test.ts` -- see its comment for why
// `vi.stubGlobal` is required for `connection.ts`'s `new WebSocket(...)` to
// pick it up. This one also supports `.open()` since this file drives the
// socket through open -> close -> open.
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  readyState = 0;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  send(data: string) {
    this.sent.push(data);
  }

  receive(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }

  close(code = 1000) {
    this.onclose?.({ code } as CloseEvent);
  }
}

function StatusProbe() {
  const status = useLiveConnectionStatus();
  return <div data-testid="status">{status}</div>;
}

function renderProvider() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LiveUpdatesProvider>
        <StatusProbe />
      </LiveUpdatesProvider>
    </QueryClientProvider>,
  );
}

describe("useLiveConnectionStatus", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reports disconnected after the socket closes and connected again after it reopens", async () => {
    const { unmount } = renderProvider();

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];

    act(() => ws.open());
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("connected"));

    act(() => ws.close());
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("disconnected"));

    act(() => ws.open());
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("connected"));

    // Unmount before the reconnect backoff timer this close scheduled can
    // fire and create another socket after the test has already finished.
    unmount();
  });
});

function Watcher({ runIds }: { runIds: string[] }) {
  useWatchRuns(runIds);
  return null;
}

describe("useWatchRuns", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("watches the runs it is given on the provider's socket", async () => {
    const qc = new QueryClient();
    const { unmount } = render(
      <QueryClientProvider client={qc}>
        <LiveUpdatesProvider>
          <Watcher runIds={["r1"]} />
        </LiveUpdatesProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    act(() => ws.open());
    expect(ws.sent.map((f) => JSON.parse(f) as unknown)).toEqual([
      { type: "run.watch", runId: "r1" },
    ]);
    unmount();
  });

  it("refetches a run whose watch the server dropped", async () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    const { unmount } = render(
      <QueryClientProvider client={qc}>
        <LiveUpdatesProvider>
          <Watcher runIds={["r1"]} />
        </LiveUpdatesProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    act(() => ws.open());
    spy.mockClear();
    act(() => ws.receive({ type: "run.unwatched", runId: "r1", reason: "access" }));
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(["run", "r1"]));
    unmount();
  });

  it("is a no-op outside the provider", () => {
    expect(() => render(<Watcher runIds={["r1"]} />)).not.toThrow();
  });
});

function ChatProbe({ fetches }: { fetches: { count: number } }) {
  useQuery({
    queryKey: ["chat", "messages", "s1"],
    queryFn: () => {
      fetches.count += 1;
      return Promise.resolve([]);
    },
  });
  return null;
}

describe("reconnect resync", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("refetches the chat transcript and session caches after the socket reopens", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(qc, "invalidateQueries");
    const fetches = { count: 0 };
    const { unmount } = render(
      <QueryClientProvider client={qc}>
        <LiveUpdatesProvider>
          <ChatProbe fetches={fetches} />
        </LiveUpdatesProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetches.count).toBe(1));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    act(() => ws.open());
    act(() => ws.close());
    spy.mockClear();
    const before = fetches.count;
    act(() => ws.open());
    await waitFor(() => expect(fetches.count).toBe(before + 1));
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toEqual(
      expect.arrayContaining([
        JSON.stringify(["chat", "messages"]),
        JSON.stringify(["chat", "session"]),
        JSON.stringify(["chat", "sessions"]),
      ]),
    );
    unmount();
  });
});
