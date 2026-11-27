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
        { agentId: "a1", agentName: "Nora", model: "gpt-big", up: 3, down: 1 },
        { agentId: "a2", agentName: "Sina", model: null, up: 0, down: 2 },
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
    expect(nora.getByText("75 %")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Sina")).toBeInTheDocument();
    expect(within(rows[1]).getByText("0 %")).toBeInTheDocument();
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
  });

  it("says so when the counts could not be loaded", () => {
    feedbackMock.mockReturnValue({ data: undefined, isError: true, isPending: false });
    renderPage();

    expect(screen.getByText("Feedback could not be loaded.")).toBeInTheDocument();
  });
});
