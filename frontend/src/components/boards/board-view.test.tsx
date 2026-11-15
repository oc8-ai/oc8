import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import * as boardsApi from "@/lib/boards-api";
import type { Board, BoardBlock, BoardQuery, QueryResult } from "@/lib/boards-api";
import { BoardView } from "./board-view";
import { DEFAULT_FILTERS } from "./board-filters";
import { chartData } from "./chart-data";

const today = new Date("2026-11-15T12:00:00Z");

function result(over: Partial<QueryResult>): QueryResult {
  return {
    series: [],
    totals: [],
    compare: null,
    rows: null,
    totalRows: null,
    note: null,
    ...over,
  };
}

function board(blocks: BoardBlock[]): Board {
  return {
    id: "b1",
    title: "Sales board",
    description: "",
    departmentId: "d1",
    agentId: "a1",
    agentName: "Sales agent",
    stale: false,
    recordCount: 10,
    lastRecordedAt: null,
    mayDelete: true,
    specVersion: 1,
    spec: {
      datasets: [
        {
          key: "sales",
          label: "Sales",
          grain: "day",
          fields: [
            { key: "amount", label: "Amount", type: "money", currency: "EUR" },
            { key: "margin", label: "Margin", type: "percent" },
            { key: "region", label: "Region", type: "category" },
          ],
        },
      ],
      blocks,
    },
  };
}

type Answer = {
  data?: QueryResult;
  error?: unknown;
  isPending?: boolean;
  isPlaceholderData?: boolean;
};

function mockQueries(answer: (q: BoardQuery) => Answer) {
  const spy = vi.spyOn(boardsApi, "useBoardQuery");
  spy.mockImplementation((_id: string, q: BoardQuery | null) => {
    const a = q ? answer(q) : {};
    return {
      data: a.data,
      error: a.error ?? null,
      isPending: a.isPending ?? (!a.data && !a.error),
      isError: !!a.error,
      isPlaceholderData: a.isPlaceholderData ?? false,
    } as never;
  });
  return spy;
}

function renderView(blocks: BoardBlock[], onFiltersChange = vi.fn()) {
  render(
    <BoardView
      board={board(blocks)}
      filters={DEFAULT_FILTERS}
      onFiltersChange={onFiltersChange}
      today={today}
    />,
  );
  return onFiltersChange;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("BoardView", () => {
  it("renders a KPI with currency and change", () => {
    const spy = mockQueries(() => ({
      data: result({
        totals: [{ group: null, values: { amount: 120 } }],
        compare: {
          period: { from: "2026-10-01", to: "2026-11-01" },
          series: [],
          totals: [{ group: null, values: { amount: 100 } }],
          change: [{ group: null, values: { amount: 0.2 } }],
        },
      }),
    }));
    renderView([{ type: "kpi", dataset: "sales", field: "amount", width: 3, title: "Revenue" }]);

    expect(screen.getByText("Revenue")).toBeInTheDocument();
    expect(screen.getByText(/€\s?120[.,]00/)).toBeInTheDocument();
    expect(screen.getByText("+20%")).toBeInTheDocument();
    // F14: the KPI asked for the previous period though the filters say none.
    expect(spy.mock.calls[0][1]?.compare).toBe("previous_period");
  });

  it("shows no value and no change for an empty KPI", () => {
    mockQueries(() => ({
      data: result({
        totals: [{ group: null, values: { amount: null } }],
        compare: {
          period: { from: "2026-10-01", to: "2026-11-01" },
          series: [],
          totals: [{ group: null, values: { amount: null } }],
          change: [{ group: null, values: { amount: null } }],
        },
      }),
    }));
    renderView([{ type: "kpi", dataset: "sales", field: "amount", width: 3 }]);

    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText("No data")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("formats a percent KPI with a percent sign", () => {
    mockQueries(() => ({
      data: result({ totals: [{ group: null, values: { margin: 12.5 } }] }),
    }));
    renderView([{ type: "kpi", dataset: "sales", field: "margin", width: 3 }]);
    expect(screen.getByText("12.5%")).toBeInTheDocument();
    // The default title is the field's label.
    expect(screen.getByText("Margin")).toBeInTheDocument();
  });

  it("renders one chart series per split group", () => {
    const data = result({
      series: [
        { asOf: "2026-11-01", group: "north", values: { amount: 10 } },
        { asOf: "2026-11-01", group: "south", values: { amount: 5 } },
        { asOf: "2026-11-02", group: "north", values: { amount: 7 } },
      ],
    });
    mockQueries(() => ({ data }));
    renderView([
      { type: "bar", dataset: "sales", fields: ["amount"], split_by: "region", width: 6 },
    ]);

    const legend = screen.getByRole("list", { name: "Legend" });
    expect(within(legend).getAllByRole("listitem")).toHaveLength(2);
    expect(within(legend).getByText("north")).toBeInTheDocument();
    expect(within(legend).getByText("south")).toBeInTheDocument();

    const built = chartData(
      data,
      { type: "bar", dataset: "sales", fields: ["amount"], split_by: "region", width: 6 },
      board([]).spec.datasets[0],
    );
    expect(built.series.map((s) => s.label)).toEqual(["north", "south"]);
    expect(built.rows).toEqual([
      { asOf: "2026-11-01", s0: 10, s1: 5 },
      { asOf: "2026-11-02", s0: 7 },
    ]);
  });

  it("renders one chart series per field without a split", () => {
    const built = chartData(
      result({
        series: [{ asOf: "2026-11-01", group: null, values: { amount: 10, margin: 3 } }],
      }),
      { type: "line", dataset: "sales", fields: ["amount", "margin"], width: 6 },
      board([]).spec.datasets[0],
    );
    expect(built.series.map((s) => s.label)).toEqual(["Amount", "Margin"]);
    expect(built.rows).toEqual([{ asOf: "2026-11-01", s0: 10, s1: 3 }]);
  });

  it("renders the note with its date", () => {
    mockQueries(() => ({
      data: result({
        rows: [],
        totalRows: 0,
        note: {
          text: "Revenue up 8 %",
          asOf: "2026-11-10T00:00:00+00:00",
          recordedAt: "2026-11-10T06:00:00+00:00",
        },
      }),
    }));
    renderView([{ type: "note", dataset: "sales", width: 6 }]);

    expect(screen.getByText("Revenue up 8 %")).toBeInTheDocument();
    expect(screen.getByText("Nov 10, 2026")).toBeInTheDocument();
  });

  it("shows the Enterprise placeholder for an artifact block", () => {
    const spy = mockQueries(() => ({}));
    renderView([{ type: "artifact", artifact_ref: "canvas", datasets: ["sales"], width: 12 }]);
    expect(screen.getByText(/available in Enterprise/i)).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it("shows a neutral message, never the raw detail, for a refused query", () => {
    mockQueries(() => ({ error: new ApiError("board_query:not_numeric: amount", 422) }));
    renderView([{ type: "kpi", dataset: "sales", field: "amount", width: 3 }]);
    expect(screen.getByText("This block can't be shown")).toBeInTheDocument();
    expect(screen.queryByText(/not_numeric/)).not.toBeInTheDocument();
  });

  it("says the dashboard was deleted on a 410", () => {
    mockQueries(() => ({ error: new ApiError("board removed", 410) }));
    renderView([{ type: "kpi", dataset: "sales", field: "amount", width: 3 }]);
    expect(screen.getByText("This dashboard was deleted")).toBeInTheDocument();
  });

  it("places blocks on a 12-column grid, full width on a phone", () => {
    mockQueries(() => ({ data: result({ totals: [{ group: null, values: { amount: 1 } }] }) }));
    renderView([
      { type: "kpi", dataset: "sales", field: "amount", width: 3 },
      { type: "kpi", dataset: "sales", field: "amount", width: 8 },
    ]);
    const cells = screen.getAllByTestId("board-block");
    expect(cells[0].parentElement).toHaveClass("grid-cols-12");
    expect(cells[0]).toHaveClass("col-span-12", "md:col-span-3");
    expect(cells[1]).toHaveClass("col-span-12", "md:col-span-8");
  });

  it("sorts a table by a header and pages on the server", () => {
    const spy = mockQueries(() => ({
      data: result({
        rows: [{ as_of: "2026-11-02T00:00:00+00:00", amount: 42, region: "north" }],
        totalRows: 30,
      }),
    }));
    renderView([
      { type: "table", dataset: "sales", fields: ["amount", "region"], page_size: 10, width: 12 },
    ]);

    expect(screen.getByText("north")).toBeInTheDocument();
    expect(screen.getByText(/€\s?42[.,]00/)).toBeInTheDocument();
    expect(screen.getByText("1–1 of 30")).toHaveAttribute("aria-live", "polite");
    // Newest first until a header is clicked.
    expect(spy.mock.lastCall?.[1]?.sort).toEqual({ field: "as_of", dir: "desc" });
    expect(screen.getByRole("columnheader", { name: /Date/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(screen.getByText("north").closest("tr")?.className ?? "").not.toMatch(/border/);

    fireEvent.click(screen.getByRole("button", { name: /Amount/ }));
    expect(spy.mock.lastCall?.[1]?.sort).toEqual({ field: "amount", dir: "asc" });
    fireEvent.click(screen.getByRole("button", { name: /Amount/ }));
    expect(spy.mock.lastCall?.[1]?.sort).toEqual({ field: "amount", dir: "desc" });

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(spy.mock.lastCall?.[1]?.page).toEqual({ offset: 10, limit: 10 });
    expect(screen.getByRole("button", { name: "Previous page" })).toBeEnabled();
  });

  it("changes the preset, the comparison and the reporting from the filter bar", () => {
    mockQueries((q) =>
      q.groupBy === "region"
        ? {
            data: result({
              totals: [
                { group: "south", values: { amount: 1 } },
                { group: "north", values: { amount: 2 } },
              ],
            }),
          }
        : {},
    );
    const onChange = renderView([
      {
        type: "filter_bar",
        width: 12,
        presets: ["this_month", "last_12_months"],
        comparisons: ["previous_period"],
        category_filters: [{ dataset: "sales", field: "region" }],
      },
    ]);

    const preset = screen.getByLabelText("Period");
    expect(within(preset).getAllByRole("option")).toHaveLength(2);
    fireEvent.change(preset, { target: { value: "last_12_months" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_FILTERS, preset: "last_12_months" });

    fireEvent.change(screen.getByLabelText("Compare"), { target: { value: "previous_period" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_FILTERS, compare: "previous_period" });

    fireEvent.click(screen.getByRole("switch", { name: "As first reported" }));
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_FILTERS, reported: "first" });

    expect(screen.getByRole("button", { name: "Region: All" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Region/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: "north" }));
    expect(onChange).toHaveBeenLastCalledWith({
      ...DEFAULT_FILTERS,
      categories: { region: ["north"] },
    });
  });

  it("shows date inputs for a custom range and keeps the end exclusive", () => {
    mockQueries(() => ({}));
    const onChange = vi.fn();
    render(
      <BoardView
        board={board([{ type: "filter_bar", width: 12, presets: ["custom"] }])}
        filters={{ ...DEFAULT_FILTERS, preset: "custom", from: "2025-03-01", to: "2025-04-01" }}
        onFiltersChange={onChange}
        today={today}
      />,
    );
    const to = screen.getByLabelText("To");
    // The range ends before `to`; the picker shows the last included day.
    expect(to).toHaveValue("2025-03-31");
    fireEvent.change(to, { target: { value: "2025-03-15" } });
    expect(onChange).toHaveBeenLastCalledWith({
      ...DEFAULT_FILTERS,
      preset: "custom",
      from: "2025-03-01",
      to: "2025-03-16",
    });
  });

  it("never lets a custom range invert", () => {
    mockQueries(() => ({}));
    const onChange = vi.fn();
    const filters = {
      ...DEFAULT_FILTERS,
      preset: "custom" as const,
      from: "2025-03-01",
      to: "2025-04-01",
    };
    render(
      <BoardView
        board={board([{ type: "filter_bar", width: 12, presets: ["custom"] }])}
        filters={filters}
        onFiltersChange={onChange}
        today={today}
      />,
    );
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2025-05-10" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...filters, from: "2025-05-10", to: "2025-05-11" });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2025-02-01" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...filters, from: "2025-02-01", to: "2025-02-02" });
  });

  it("names the selected categories on the filter trigger", () => {
    mockQueries(() => ({}));
    render(
      <BoardView
        board={board([
          {
            type: "filter_bar",
            width: 12,
            category_filters: [{ dataset: "sales", field: "region" }],
          },
        ])}
        filters={{ ...DEFAULT_FILTERS, categories: { region: ["north", "south"] } }}
        onFiltersChange={vi.fn()}
        today={today}
      />,
    );
    expect(screen.getByRole("button", { name: "Region: 2 selected" })).toBeInTheDocument();
  });

  it("dims a KPI and hides its change while the next period loads", () => {
    mockQueries(() => ({
      isPlaceholderData: true,
      data: result({
        totals: [{ group: null, values: { amount: 120 } }],
        compare: {
          period: { from: "2026-10-01", to: "2026-11-01" },
          series: [],
          totals: [{ group: null, values: { amount: 100 } }],
          change: [{ group: null, values: { amount: 0.2 } }],
        },
      }),
    }));
    renderView([{ type: "kpi", dataset: "sales", field: "amount", width: 3 }]);
    const value = screen.getByText(/€\s?120[.,]00/);
    expect(value.parentElement).toHaveClass("opacity-50");
    expect(value.parentElement).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("+20%")).not.toBeInTheDocument();
  });

  it("labels the page on screen, not the one loading", () => {
    let placeholder = false;
    mockQueries(() => ({
      isPlaceholderData: placeholder,
      data: result({
        rows: Array.from({ length: 10 }, (_, i) => ({
          as_of: "2026-11-02T00:00:00+00:00",
          amount: i,
          region: `r${i}`,
        })),
        totalRows: 30,
      }),
    }));
    renderView([
      { type: "table", dataset: "sales", fields: ["amount", "region"], page_size: 10, width: 12 },
    ]);
    expect(screen.getByText("1–10 of 30")).toBeInTheDocument();
    // The next page is loading: page one is still shown, so is its label.
    placeholder = true;
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("1–10 of 30")).toBeInTheDocument();
    // It arrived.
    placeholder = false;
    fireEvent.click(screen.getByRole("columnheader", { name: /Amount/ }).querySelector("button")!);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("11–20 of 30")).toBeInTheDocument();
  });
});
