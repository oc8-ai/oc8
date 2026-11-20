import { renderHook, waitFor } from "@testing-library/react";
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

import { useLiveConnection } from "@/lib/live/connection";

// jsdom's test environment provides a real global `WebSocket`, so a
// standalone fake class only gets picked up by `connection.ts`'s
// `new WebSocket(...)` call once it's installed via `vi.stubGlobal`. This
// fake supports exactly what `connection.ts` uses: assigning the four
// `on*` handlers and calling `.close()` -- nothing else in the WebSocket
// spec is needed for these tests.
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

  send(data: string) {
    this.sent.push(data);
  }

  close(code = 1000) {
    this.onclose?.({ code } as CloseEvent);
  }

  receive(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }
}

function sentFrames(ws: FakeWebSocket) {
  return ws.sent.map((s) => JSON.parse(s) as unknown);
}

describe("useLiveConnection", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("calls onClose when the socket closes", async () => {
    const onEvent = vi.fn();
    const onOpen = vi.fn();
    const onClose = vi.fn();

    const { unmount } = renderHook(() => useLiveConnection(onEvent, onOpen, onClose));

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];

    ws.close();

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();

    // Unmount before the reconnect backoff timer this close scheduled can
    // fire and create a second socket after the test has already finished.
    unmount();
  });

  it("sends run.watch once open and re-sends watches after reconnect", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { result, unmount } = renderHook(() => useLiveConnection(vi.fn(), vi.fn(), vi.fn()));
    result.current.watch("run-1");
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const first = FakeWebSocket.instances[0];
    first.readyState = 1;
    first.onopen?.();
    expect(first.sent.map((s) => JSON.parse(s))).toEqual([{ type: "run.watch", runId: "run-1" }]);

    first.close(1006);
    await vi.advanceTimersByTimeAsync(1100);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    const second = FakeWebSocket.instances[1];
    second.readyState = 1;
    second.onopen?.();
    expect(second.sent.map((s) => JSON.parse(s))).toEqual([{ type: "run.watch", runId: "run-1" }]);

    result.current.unwatch("run-1");
    second.close(1006);
    await vi.advanceTimersByTimeAsync(2100);
    const third = FakeWebSocket.instances[2];
    third.readyState = 1;
    third.onopen?.();
    expect(third.sent).toEqual([]);
    unmount();
    vi.useRealTimers();
  });

  it("sends run.watch straight away on an open socket", async () => {
    const { result, unmount } = renderHook(() => useLiveConnection(vi.fn(), vi.fn(), vi.fn()));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const ws = FakeWebSocket.instances[0];
    ws.readyState = 1;
    ws.onopen?.();
    result.current.watch("run-2");
    expect(sentFrames(ws)).toEqual([{ type: "run.watch", runId: "run-2" }]);
    unmount();
  });

  it("keeps a run watched until every watcher has let it go", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { result, unmount } = renderHook(() => useLiveConnection(vi.fn(), vi.fn(), vi.fn()));
    result.current.watch("run-1");
    result.current.watch("run-1");
    result.current.unwatch("run-1");
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const first = FakeWebSocket.instances[0];
    first.readyState = 1;
    first.onopen?.();
    expect(sentFrames(first)).toEqual([{ type: "run.watch", runId: "run-1" }]);
    unmount();
    vi.useRealTimers();
  });

  it("drops a watch the server ended and reports it, without passing control frames on", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onEvent = vi.fn();
    const onUnwatched = vi.fn();
    const { result, unmount } = renderHook(() =>
      useLiveConnection(onEvent, vi.fn(), vi.fn(), onUnwatched),
    );
    result.current.watch("run-1");
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const first = FakeWebSocket.instances[0];
    first.readyState = 1;
    first.onopen?.();
    first.receive({ type: "run.watching", runId: "run-1" });
    first.receive({ type: "run.unwatched", runId: "run-1", reason: "access" });
    expect(onEvent).not.toHaveBeenCalled();
    expect(onUnwatched).toHaveBeenCalledWith("run-1");

    first.close(1006);
    await vi.advanceTimersByTimeAsync(1100);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    const second = FakeWebSocket.instances[1];
    second.readyState = 1;
    second.onopen?.();
    expect(second.sent).toEqual([]);
    unmount();
    vi.useRealTimers();
  });
});
