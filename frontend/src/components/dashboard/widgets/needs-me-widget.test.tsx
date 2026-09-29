import { render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { NeedsMeWidget } from "./needs-me-widget";
import * as hooks from "@/lib/hooks";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }));

// `vi.spyOn(...).mockReturnValue(...)` otherwise leaks into later tests in
// this file -- there is no global mock-reset config (vitest.setup.ts).
afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const empty = { data: [], isPending: false } as never;

function mock({
  approvals = [],
  clarifications = [],
  parked = [],
  run = undefined,
}: {
  approvals?: unknown[];
  clarifications?: unknown[];
  parked?: unknown[];
  run?: unknown;
}) {
  vi.spyOn(hooks, "useApprovals").mockReturnValue({ data: approvals, isPending: false } as never);
  vi.spyOn(hooks, "useClarifications").mockReturnValue({
    data: clarifications,
    isPending: false,
  } as never);
  vi.spyOn(hooks, "useRuns").mockReturnValue({ data: parked, isPending: false } as never);
  vi.spyOn(hooks, "useRun").mockReturnValue({ data: run, isPending: false } as never);
}

describe("NeedsMeWidget", () => {
  it("says so plainly when nothing is waiting", () => {
    mock({});
    render(<NeedsMeWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByText(/Nothing is waiting/i)).toBeInTheDocument();
  });

  it("merges approvals, questions and parked runs into one list", () => {
    mock({
      approvals: [
        {
          id: "ap1",
          title: "Approve the Acme offer",
          runId: "r1",
          createdAt: "2026-09-27T10:00:00Z",
        },
      ],
      clarifications: [
        { id: "cl1", runId: "r2", question: "Which account?", createdAt: "2026-09-27T09:00:00Z" },
      ],
      parked: [{ id: "r3", agentId: "a1", state: "waiting_for_approval", steps: 4, toolCalls: [] }],
    });
    render(<NeedsMeWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByText("Approve the Acme offer")).toBeInTheDocument();
    expect(screen.getByText("Which account?")).toBeInTheDocument();
    expect(screen.getByText(/nobody was asked/i)).toBeInTheDocument();
  });

  it("does not list a parked run twice when an approval already covers it", () => {
    mock({
      approvals: [{ id: "ap1", title: "Approve the Acme offer", runId: "r1", createdAt: "" }],
      parked: [{ id: "r1", agentId: "a1", state: "waiting_for_approval", steps: 4, toolCalls: [] }],
    });
    render(<NeedsMeWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.queryByText(/nobody was asked/i)).not.toBeInTheDocument();
  });

  it("puts the item that blocks the most work first", () => {
    mock({
      approvals: [
        { id: "ap-none", title: "Budget exhausted", createdAt: "2026-09-27T12:00:00Z" },
        {
          id: "ap-run",
          title: "Approve the Acme offer",
          runId: "r1",
          createdAt: "2026-09-27T08:00:00Z",
        },
      ],
      parked: [{ id: "r9", agentId: "a1", state: "waiting_for_input", steps: 2, toolCalls: [] }],
    });
    render(<NeedsMeWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    const titles = screen.getAllByRole("button").map((b) => b.textContent ?? "");
    // A run nobody was asked about, then the approval holding a run, then the
    // one holding nothing -- newest-first only breaks ties within a weight.
    expect(titles[0]).toMatch(/nobody was asked/i);
    expect(titles[1]).toMatch(/Acme/);
    expect(titles[2]).toMatch(/Budget exhausted/);
  });

  it("shows the blocked run's own steps inside the row", () => {
    mock({
      approvals: [{ id: "ap1", title: "Approve the Acme offer", runId: "r1", createdAt: "" }],
      run: {
        id: "r1",
        agentId: "a1",
        state: "waiting_for_approval",
        steps: 3,
        toolCalls: [
          {
            tool: "memory_write",
            arguments: {},
            result: "ok",
            step: 1,
            connection: null,
            state: "done",
          },
        ],
        stepTimings: [{ step: 1, stepWallMs: 400 }],
      },
    });
    render(<NeedsMeWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByText("Approve the Acme offer"));
    expect(screen.getByTestId("run-step-timeline")).toBeInTheDocument();
    expect(screen.getByText("Made a note")).toBeInTheDocument();
  });

  it("fetches no run for a collapsed row", () => {
    mock({
      approvals: [{ id: "ap1", title: "Approve the Acme offer", runId: "r1", createdAt: "" }],
    });
    render(<NeedsMeWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(hooks.useRun).not.toHaveBeenCalled();
  });
});
