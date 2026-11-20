import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: (url: string) => getMock(url), post: vi.fn() } };
});

import { LiveLog } from "@/routes/agents.$id";
import { StatusPill } from "@/components/app-shell";
import { RunStateBadge } from "@/components/run-state-badge";
import { applyEvent } from "@/lib/live/apply-event";
import type { RealtimeEvent } from "@/lib/live/types";

let n = 0;
function status(state: string): RealtimeEvent {
  n += 1;
  return {
    id: `e${n}`,
    type: "run.status",
    tenantid: "t1",
    time: "2026-11-04T00:00:00Z",
    source: "test",
    data: { run_id: "r1", agent_id: "a1", state, phase: null },
  };
}

// What GET /runs/{id} answers. An in-process run commits its RUNNING
// transition only when it ends, so the server keeps saying "queued" for the
// whole run -- the exact situation the badge used to get wrong.
let serverState = "queued";

function route(url: string) {
  if (url === "/runs/r1") {
    return Promise.resolve({
      id: "r1",
      agentId: "a1",
      state: serverState,
      steps: 0,
      toolCalls: [],
      updatedAt: new Date().toISOString(),
    });
  }
  return Promise.resolve([]);
}

function renderLiveLog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <LiveLog agentId="a1" runId="r1" onOpenVersions={() => {}} />
    </QueryClientProvider>,
  );
  return qc;
}

const badge = () => screen.getByTestId("run-state-badge");

describe("Live Log run state badge", () => {
  beforeEach(() => {
    serverState = "queued";
    getMock.mockReset();
    getMock.mockImplementation(route);
  });

  it("follows the run through every transition, even while the server lags", async () => {
    const qc = renderLiveLog();
    await waitFor(() => expect(badge().textContent).toBe("queued"));

    act(() => applyEvent(qc, status("running")));
    await waitFor(() => expect(badge().textContent).toBe("running"));

    // A poll in between still reads the uncommitted "queued": it must not win.
    await act(() => qc.invalidateQueries({ queryKey: ["run", "r1"] }));
    expect(getMock.mock.calls.filter(([u]) => u === "/runs/r1").length).toBeGreaterThan(1);
    // Let the refetched data reach the screen before looking at it.
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(badge().textContent).toBe("running");

    act(() => applyEvent(qc, status("waiting_for_input")));
    await waitFor(() => expect(badge().textContent).toBe("waiting for input"));

    act(() => applyEvent(qc, status("running")));
    await waitFor(() => expect(badge().textContent).toBe("running"));

    serverState = "done";
    act(() => applyEvent(qc, status("done")));
    await waitFor(() => expect(badge().textContent).toBe("done"));
  });

  it("keeps an event that arrived before the first fetch answered", async () => {
    let answer: (v: unknown) => void = () => {};
    getMock.mockImplementation((url: string) =>
      url === "/runs/r1" ? new Promise((resolve) => (answer = resolve)) : Promise.resolve([]),
    );
    const qc = renderLiveLog();
    act(() => applyEvent(qc, status("running")));
    await act(async () =>
      answer({ id: "r1", agentId: "a1", state: "queued", steps: 0, toolCalls: [], updatedAt: "" }),
    );
    await waitFor(() => expect(badge().textContent).toBe("running"));
  });

  it.each(["failed", "interrupted"])("shows %s when the run ends that way", async (end) => {
    const qc = renderLiveLog();
    await waitFor(() => expect(badge().textContent).toBe("queued"));
    act(() => applyEvent(qc, status("running")));
    act(() => applyEvent(qc, status(end)));
    await waitFor(() => expect(badge().textContent).toBe(end));
  });

  it("uses the header's word and colour for a running agent", () => {
    render(
      <>
        <StatusPill status="running" />
        <RunStateBadge state="running" />
      </>,
    );
    const [header, panel] = screen.getAllByText("running");
    expect(panel.style.color).toBe(header.style.color);
    expect(panel.className).toBe(header.className);
  });
});
