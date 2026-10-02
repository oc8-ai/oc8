import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ActivityWidget } from "./activity-widget";
import * as hooks from "@/lib/hooks";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const noRuns = { data: [], isPending: false } as never;

describe("ActivityWidget", () => {
  it("shows at most the 8 most recent entries", () => {
    const items = Array.from({ length: 12 }, (_, i) => ({
      id: `e${i}`,
      message: `Event ${i}`,
      agentId: "agent-1",
      status: "success" as const,
      time: new Date().toISOString(),
    }));
    vi.spyOn(hooks, "useActivity").mockReturnValue({ data: items, isPending: false } as never);
    vi.spyOn(hooks, "useRuns").mockReturnValue(noRuns);

    render(<ActivityWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByText("Event 0")).toBeInTheDocument();
    expect(screen.queryByText("Event 8")).not.toBeInTheDocument();
  });

  it("shows a step timeline per recent run, above the feed", () => {
    vi.spyOn(hooks, "useActivity").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useRuns").mockReturnValue({
      data: [
        {
          id: "r1",
          agentId: "a1",
          state: "done",
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
          stepTimings: [{ step: 1, stepWallMs: 500 }],
        },
      ],
      isPending: false,
    } as never);

    render(<ActivityWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByTestId("run-step-timeline")).toBeInTheDocument();
    expect(screen.getByText(/3 steps/)).toBeInTheDocument();
  });

  it("shows at most four runs", () => {
    vi.spyOn(hooks, "useActivity").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useRuns").mockReturnValue({
      data: Array.from({ length: 9 }, (_, i) => ({
        id: `r${i}`,
        agentId: "a1",
        state: "done",
        steps: 1,
        toolCalls: [],
      })),
      isPending: false,
    } as never);

    render(<ActivityWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getAllByTestId("run-step-timeline")).toHaveLength(4);
  });
});
