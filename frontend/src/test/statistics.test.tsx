import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const tenantKpisMock = vi.fn();
const agentsMock = vi.fn();
const departmentsMock = vi.fn();

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useTenantKpis: (...args: unknown[]) => tenantKpisMock(...args),
    useAgents: (...args: unknown[]) => agentsMock(...args),
    useDepartments: (...args: unknown[]) => departmentsMock(...args),
    // The feedback table has its own test (statistics-feedback.test.tsx).
    useFeedbackCounts: () => ({ data: [], isError: false, isPending: false }),
  };
});

import { StatisticsPage } from "@/routes/statistics";

function row(groupKey: string, runCount: number, overrides: Record<string, number | null> = {}) {
  return {
    groupKey,
    runCount,
    totalDurationMs: null,
    executionDurationMs: null,
    approvalWaitMs: null,
    responseTimeMs: null,
    avgToolCallDurationMs: null,
    ...overrides,
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <StatisticsPage />
    </QueryClientProvider>,
  );
}

/** The shape `useTenantKpis` hands back when nothing has gone wrong. */
function ok(rows: ReturnType<typeof row>[]) {
  return { data: { rows }, isError: false, error: null, isPending: false };
}

describe("Statistics page", () => {
  beforeEach(() => {
    tenantKpisMock.mockReset();
    agentsMock.mockReset();
    departmentsMock.mockReset();
    tenantKpisMock.mockReturnValue(ok([]));
    agentsMock.mockReturnValue({
      data: {
        items: [
          { id: "agent-1", name: "Nora" },
          { id: "agent-2", name: "Sina" },
        ],
        totalCount: 2,
      },
    });
    departmentsMock.mockReturnValue({
      data: { items: [{ id: "dept-1", name: "Vertrieb" }], totalCount: 1 },
    });
  });

  it("defaults to groupBy=agent with no date bounds on first load", () => {
    renderPage();

    // Deliberate: the backend caps a bucketed grouping at MAX_BUCKETS and an
    // enumerating one at MAX_GROUPS. "agent" is the safe first-load default.
    expect(tenantKpisMock).toHaveBeenCalledWith({
      agentId: undefined,
      departmentId: undefined,
      dateFrom: undefined,
      dateTo: undefined,
      status: undefined,
      groupBy: "agent",
    });
  });

  it("renders one table row per KPI row, resolving agent ids to names", () => {
    tenantKpisMock.mockReturnValue(
      ok([
        row("agent-1", 42, { totalDurationMs: 200_000, approvalWaitMs: 1_200 }),
        row("agent-2", 7),
      ]),
    );
    renderPage();

    const kpiRows = screen.getAllByTestId("kpi-row");
    expect(kpiRows).toHaveLength(2);
    // Scoped to the table: "Nora"/"Sina" are also the agent picker's <option>
    // labels, so an unscoped getByText would match twice.
    expect(within(kpiRows[0]).getByText("Nora")).toBeInTheDocument();
    expect(within(kpiRows[0]).getByText("42")).toBeInTheDocument();
    expect(within(kpiRows[1]).getByText("Sina")).toBeInTheDocument();
    expect(within(kpiRows[1]).getByText("7")).toBeInTheDocument();
    // formatMs, shared with Tasks 7/8.
    expect(within(kpiRows[0]).getByText("3m 20s")).toBeInTheDocument();
    expect(within(kpiRows[0]).getByText("1.2s")).toBeInTheDocument();
  });

  it("shows a date bucket's own key as-is when grouping by day", () => {
    tenantKpisMock.mockReturnValue(ok([row("2026-08-01", 3)]));
    renderPage();
    fireEvent.change(screen.getByLabelText("Group by"), { target: { value: "day" } });

    expect(tenantKpisMock).toHaveBeenLastCalledWith(expect.objectContaining({ groupBy: "day" }));
    expect(screen.getByText("2026-08-01")).toBeInTheDocument();
  });

  it("passes each filter control's value through to useTenantKpis", () => {
    renderPage();

    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "agent-2" } });
    expect(tenantKpisMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ agentId: "agent-2" }),
    );

    fireEvent.change(screen.getByLabelText("Department"), { target: { value: "dept-1" } });
    expect(tenantKpisMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ agentId: "agent-2", departmentId: "dept-1" }),
    );

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-01" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-08-20" } });
    expect(tenantKpisMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ dateFrom: "2026-08-01", dateTo: "2026-08-20" }),
    );

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "failed" } });
    expect(tenantKpisMock).toHaveBeenLastCalledWith(expect.objectContaining({ status: "failed" }));
  });

  it("clearing a filter sends undefined rather than the empty string", () => {
    renderPage();
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "agent-2" } });
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "" } });

    expect(tenantKpisMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ agentId: undefined }),
    );
  });

  it("renders each of the three chart types when toggled, without throwing", () => {
    tenantKpisMock.mockReturnValue(ok([row("agent-1", 42), row("agent-2", 7)]));
    renderPage();

    expect(screen.getByTestId("chart-bar")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Line" }));
    expect(screen.getByTestId("chart-line")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-bar")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Pie" }));
    expect(screen.getByTestId("chart-pie")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-line")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Bar" }));
    expect(screen.getByTestId("chart-bar")).toBeInTheDocument();
  });

  it("explains a 422 cap rejection instead of crashing or showing a blank table", () => {
    // GET /kpis rejects rather than truncating past MAX_BUCKETS/MAX_GROUPS.
    const err = Object.assign(new Error("groupBy=day would produce more than 100 buckets"), {
      status: 422,
    });
    tenantKpisMock.mockReturnValue({ data: undefined, isError: true, error: err });

    expect(() => renderPage()).not.toThrow();
    expect(
      screen.getByText(
        "This filter produces too many results. Narrow the date range, choose a coarser grouping, or filter by an agent or department.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryAllByTestId("kpi-row")).toHaveLength(0);
    expect(screen.queryByTestId("chart-bar")).not.toBeInTheDocument();
  });

  it("falls back to a generic message for a non-422 failure", () => {
    tenantKpisMock.mockReturnValue({
      data: undefined,
      isError: true,
      error: Object.assign(new Error("boom"), { status: 500 }),
    });

    expect(() => renderPage()).not.toThrow();
    expect(screen.getByText("Statistics could not be loaded.")).toBeInTheDocument();
  });

  it("says so when the tenant has no rows for this filter", () => {
    tenantKpisMock.mockReturnValue(ok([]));
    renderPage();

    expect(screen.getByText("No data for this filter.")).toBeInTheDocument();
  });

  it("says it is loading rather than claiming there is no data", () => {
    tenantKpisMock.mockReturnValue({ data: undefined, isError: false, isPending: true });
    renderPage();

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("No data for this filter.")).not.toBeInTheDocument();
  });
});
