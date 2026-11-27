import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const feedbackMock = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useTenantKpis: () => ({ data: { rows: [] }, isError: false, error: null, isPending: false }),
    useAgents: () => ({ data: { items: [], totalCount: 0 } }),
    useDepartments: () => ({ data: { items: [], totalCount: 0 } }),
    useFeedbackCounts: (...args: unknown[]) => feedbackMock(...args),
  };
});

import { StatisticsPage } from "@/routes/statistics";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <StatisticsPage />
    </QueryClientProvider>,
  );
}

describe("Statistics page: answer feedback", () => {
  beforeEach(() => {
    feedbackMock.mockReset();
  });

  it("shows up, down and the positive share per agent and model", () => {
    feedbackMock.mockReturnValue({
      data: [
        { agentId: "a1", agentName: "Nora", isAssistant: false, model: "gpt-big", up: 3, down: 1 },
        { agentId: "a2", agentName: "Sina", isAssistant: false, model: null, up: 0, down: 2 },
      ],
      isError: false,
      isPending: false,
    });
    renderPage();

    expect(screen.getByText("Answer feedback")).toBeInTheDocument();
    const rows = screen.getAllByTestId("feedback-row");
    expect(rows).toHaveLength(2);
    const nora = within(rows[0]);
    expect(nora.getByText("Nora")).toBeInTheDocument();
    expect(nora.getByText("gpt-big")).toBeInTheDocument();
    expect(nora.getByText("3")).toBeInTheDocument();
    expect(nora.getByText("75%")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Sina")).toBeInTheDocument();
    expect(within(rows[1]).getByText("0%")).toBeInTheDocument();
    // A labelled table with a row header per agent.
    expect(screen.getByRole("table", { name: "Answer feedback" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Nora" })).toBeInTheDocument();
  });

  it("names the Copilot's rows Copilot, not by its stored agent name", () => {
    feedbackMock.mockReturnValue({
      data: [
        {
          agentId: "c1",
          agentName: "oc8 Assistant",
          isAssistant: true,
          model: "gpt-big",
          up: 2,
          down: 0,
        },
      ],
      isError: false,
      isPending: false,
    });
    renderPage();

    expect(screen.getByRole("rowheader", { name: "Copilot" })).toBeInTheDocument();
    expect(screen.queryByText("oc8 Assistant")).not.toBeInTheDocument();
  });

  it("asks with the page's agent, department and period filters only", () => {
    feedbackMock.mockReturnValue({ data: [], isError: false, isPending: false });
    renderPage();

    expect(feedbackMock).toHaveBeenCalledWith({
      agentId: undefined,
      departmentId: undefined,
      dateFrom: undefined,
      dateTo: undefined,
    });
    expect(screen.getByText("No feedback for this filter.")).toBeInTheDocument();
    // The message stands in for the table, not below an empty one.
    expect(screen.queryByRole("table", { name: "Answer feedback" })).not.toBeInTheDocument();
  });

  it("says so when the counts could not be loaded", () => {
    feedbackMock.mockReturnValue({
      data: undefined,
      isError: true,
      error: new Error("network"),
      isPending: false,
    });
    renderPage();

    expect(screen.getByText("Feedback could not be loaded.")).toBeInTheDocument();
  });

  it("says feedback is not counted when the filter names an uncounted agent", () => {
    feedbackMock.mockReturnValue({
      data: undefined,
      isError: true,
      error: Object.assign(new Error("agent not found"), { status: 404 }),
      isPending: false,
    });
    renderPage();

    expect(screen.getByText("Feedback is not counted for this agent.")).toBeInTheDocument();
    expect(screen.queryByText("Feedback could not be loaded.")).not.toBeInTheDocument();
  });
});
