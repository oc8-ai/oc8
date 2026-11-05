import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: (url: string) => getMock(url), post: vi.fn() } };
});

import { HeaderStatusPill } from "@/routes/agents.$id";
import { StatusPill } from "@/components/app-shell";
import { RunStateBadge } from "@/components/run-state-badge";
import { RUN_STATE_COLOR } from "@/lib/run-state";
import { applyEvent } from "@/lib/live/apply-event";
import type { RealtimeEvent } from "@/lib/live/types";

let n = 0;
function status(runId: string, state: string): RealtimeEvent {
  n += 1;
  return {
    id: `h${n}`,
    type: "run.status",
    tenantid: "t1",
    time: "2026-11-04T00:00:00Z",
    source: "test",
    data: { run_id: runId, agent_id: "a1", state, phase: null },
  };
}

function summary(id: string, state: string) {
  return {
    id,
    agentId: "a1",
    state,
    source: "manual",
    createdAt: "2026-11-04T00:00:00Z",
    startedAt: null,
    finishedAt: null,
    durationMs: null,
    steps: 0,
    taskExcerpt: null,
    outputExcerpt: null,
    failureReason: null,
  };
}

let listed: ReturnType<typeof summary>[] = [];

function route(url: string) {
  if (url.startsWith("/agents/a1/runs")) {
    return Promise.resolve({ items: listed, totalCount: listed.length });
  }
  return Promise.resolve([]);
}

function renderHeader() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <HeaderStatusPill agentId="a1" status="waiting_for_task" runId={null} />
    </QueryClientProvider>,
  );
  return qc;
}

const pill = () => screen.getByText(/./, { selector: "span.rounded-full" });

describe("agent header status pill", () => {
  beforeEach(() => {
    listed = [];
    getMock.mockReset();
    getMock.mockImplementation(route);
  });

  it("shows the agent's own status while no run is active", async () => {
    renderHeader();
    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(screen.getByText("Waiting for task")).toBeTruthy();
  });

  it("shows the live run state during a run and falls back afterwards", async () => {
    listed = [summary("r1", "queued")];
    const qc = renderHeader();
    await waitFor(() => expect(pill().textContent).toBe("queued"));

    act(() => applyEvent(qc, status("r1", "running")));
    await waitFor(() => expect(pill().textContent).toBe("running"));

    act(() => applyEvent(qc, status("r1", "waiting_for_input")));
    await waitFor(() => expect(pill().textContent).toBe("waiting for input"));

    listed = [summary("r1", "done")];
    act(() => applyEvent(qc, status("r1", "done")));
    await waitFor(() => expect(screen.getByText("Waiting for task")).toBeTruthy());
  });

  it("shows the most relevant of several concurrent runs", async () => {
    listed = [
      summary("r1", "queued"),
      summary("r2", "waiting_for_input"),
      summary("r3", "running"),
    ];
    renderHeader();
    await waitFor(() => expect(pill().textContent).toBe("running"));
  });
});

describe("run state colours", () => {
  it("are the same wherever a state is shown", () => {
    const expected: Record<string, string> = {
      done: "var(--status-waiting-for-task)",
      running: "var(--status-running)",
      waiting_for_input: "var(--status-warning)",
      waiting_for_approval: "var(--status-warning)",
      failed: "var(--status-error)",
      queued: "var(--status-paused)",
      interrupted: "var(--status-paused)",
    };
    for (const [state, color] of Object.entries(expected)) {
      expect(RUN_STATE_COLOR[state]).toBe(color);
      const { unmount } = render(<RunStateBadge state={state} />);
      expect(screen.getByTestId("run-state-badge").style.color).toBe(color);
      unmount();
    }
  });

  it("gives the header's agent pill the same colour as the run badge for the same word", () => {
    render(
      <>
        <StatusPill status="running" />
        <RunStateBadge state="running" />
      </>,
    );
    const [header, panel] = screen.getAllByText("running");
    expect(panel.style.color).toBe(header.style.color);
  });
});
