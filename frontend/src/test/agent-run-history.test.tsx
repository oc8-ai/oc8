import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: (url: string) => getMock(url), post: vi.fn() } };
});

import { AgentRunHistory } from "@/routes/agents.$id";
import type { RunSummaryDTO } from "@/lib/hooks";

function summary(i: number, over: Partial<RunSummaryDTO> = {}): RunSummaryDTO {
  return {
    id: `r${i}`,
    agentId: "a1",
    state: "done",
    source: "manual",
    createdAt: "2026-11-01T09:00:00Z",
    startedAt: "2026-11-01T09:00:05Z",
    finishedAt: "2026-11-01T09:00:17Z",
    durationMs: 12_000,
    steps: 1,
    taskExcerpt: `Task number ${i}`,
    outputExcerpt: null,
    failureReason: null,
    ...over,
  };
}

const FIRST_PAGE = [
  summary(1, {
    state: "failed",
    source: "cron",
    failureReason: "Model exceeded its token budget",
  }),
  ...Array.from({ length: 19 }, (_, k) => summary(k + 2)),
];
const SECOND_PAGE = Array.from({ length: 5 }, (_, k) => summary(k + 21));

const FAILED_RUN = {
  id: "r1",
  agentId: "a1",
  state: "failed",
  steps: 1,
  toolCalls: [{ tool: "search_records", state: "done", output: "3 records" }],
  failureReason: "Model exceeded its token budget",
  output: "Model exceeded its token budget",
  updatedAt: "2026-11-01T09:00:17Z",
  stepTimings: [
    { step: 1, tokensIn: 900, tokensOut: 1536, maxTokens: 1536, reasoning: "Weigh the inbox." },
  ],
};

function renderHistory() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <AgentRunHistory agentId="a1" onOpenVersions={() => {}} />
    </QueryClientProvider>,
  );
}

describe("AgentRunHistory", () => {
  beforeEach(() => {
    getMock.mockReset();
    getMock.mockImplementation((url: string) => {
      if (url === "/agents/a1/runs?limit=20&offset=0")
        return Promise.resolve({ items: FIRST_PAGE, totalCount: 25 });
      if (url === "/agents/a1/runs?limit=20&offset=20")
        return Promise.resolve({ items: SECOND_PAGE, totalCount: 25 });
      if (url === "/runs/r1") return Promise.resolve(FAILED_RUN);
      return Promise.reject(new Error(`unexpected ${url}`));
    });
  });

  it("lists the runs newest first with state, duration, trigger and excerpt", async () => {
    renderHistory();
    const rows = await screen.findAllByTestId("run-history-row");
    expect(rows).toHaveLength(20);
    const first = within(rows[0]);
    expect(first.getByTestId("run-state-badge").textContent).toBe("failed");
    expect(first.getByText("12.0s")).toBeTruthy();
    expect(first.getByText("schedule")).toBeTruthy();
    expect(first.getByText("Task number 1")).toBeTruthy();
    expect(screen.getByTestId("history-range").textContent).toBe("1–20 of 25");
  });

  it("pages to older runs and back", async () => {
    renderHistory();
    await screen.findAllByTestId("run-history-row");
    expect((screen.getByRole("button", { name: "Newer" }) as HTMLButtonElement).disabled).toBe(
      true,
    );

    fireEvent.click(screen.getByRole("button", { name: "Older" }));
    await waitFor(() => expect(screen.getAllByTestId("run-history-row")).toHaveLength(5));
    expect(getMock).toHaveBeenCalledWith("/agents/a1/runs?limit=20&offset=20");
    expect(screen.getByTestId("history-range").textContent).toBe("21–25 of 25");
    expect((screen.getByRole("button", { name: "Older" }) as HTMLButtonElement).disabled).toBe(
      true,
    );

    fireEvent.click(screen.getByRole("button", { name: "Newer" }));
    await waitFor(() => expect(screen.getAllByTestId("run-history-row")).toHaveLength(20));
  });

  it("opens a run into its steps, reasoning, budget, tool calls and failure reason", async () => {
    renderHistory();
    const rows = await screen.findAllByTestId("run-history-row");
    fireEvent.click(within(rows[0]).getAllByRole("button")[0]);

    const reason = await screen.findByTestId("run-failure-reason");
    expect(reason.textContent).toContain("Model exceeded its token budget");
    expect(screen.getByText("search_records")).toBeTruthy();
    expect(screen.getByText(/budget 1,536/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Thought/ }));
    expect(screen.getByText("Weigh the inbox.")).toBeTruthy();
  });

  it("says so when the agent has never run", async () => {
    getMock.mockImplementation(() => Promise.resolve({ items: [], totalCount: 0 }));
    renderHistory();
    expect(
      await screen.findByText("This agent has not run yet. Its runs will appear here."),
    ).toBeTruthy();
    expect(screen.queryByTestId("run-history-row")).toBeNull();
    expect(screen.queryByRole("button", { name: "Older" })).toBeNull();
  });
});
