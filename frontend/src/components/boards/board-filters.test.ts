import { describe, expect, it } from "vitest";

import type { BoardBlock, BoardSpec, QueryResult } from "@/lib/boards-api";
import {
  blockQuery,
  categoryValues,
  categoryValuesQuery,
  chooseBucket,
  DEFAULT_FILTERS,
  presetPeriod,
} from "./board-filters";

const today = new Date("2026-11-15T12:00:00Z");

const kpiBlock: BoardBlock = { type: "kpi", dataset: "sales", field: "amount", width: 3 };
const chartBlock: BoardBlock = {
  type: "line",
  dataset: "sales",
  fields: ["amount"],
  split_by: "region",
  width: 6,
};
const tableBlock: BoardBlock = {
  type: "table",
  dataset: "sales",
  fields: ["amount", "region"],
  page_size: 10,
  width: 12,
};
const noteBlock: BoardBlock = { type: "note", dataset: "sales", width: 6 };
const filterBar: BoardBlock = {
  type: "filter_bar",
  width: 12,
  presets: ["this_month", "last_12_months", "year_to_date", "custom"],
  comparisons: ["previous_period", "same_period_last_year"],
  category_filters: [
    { dataset: "sales", field: "region" },
    { dataset: "tickets", field: "team" },
  ],
};
const artifactBlock: BoardBlock = {
  type: "artifact",
  artifact_ref: "canvas-1",
  datasets: ["sales"],
  width: 12,
};

const spec: BoardSpec = {
  datasets: [
    {
      key: "sales",
      label: "Sales",
      grain: "day",
      fields: [
        { key: "amount", label: "Amount", type: "money", currency: "EUR" },
        { key: "region", label: "Region", type: "category" },
      ],
    },
    {
      key: "tickets",
      label: "Tickets",
      grain: "day",
      fields: [
        { key: "team", label: "Team", type: "category" },
        { key: "title", label: "Title", type: "text" },
      ],
    },
  ],
  blocks: [filterBar, kpiBlock, chartBlock, tableBlock, noteBlock, artifactBlock],
};

describe("presetPeriod", () => {
  it("resolves each preset to a half-open date range", () => {
    expect(presetPeriod({ ...DEFAULT_FILTERS, preset: "this_month" }, today)).toEqual({
      from: "2026-11-01",
      to: "2026-12-01",
    });
    expect(presetPeriod({ ...DEFAULT_FILTERS, preset: "last_12_months" }, today)).toEqual({
      from: "2025-12-01",
      to: "2026-12-01",
    });
    expect(presetPeriod({ ...DEFAULT_FILTERS, preset: "year_to_date" }, today)).toEqual({
      from: "2026-01-01",
      to: "2026-11-16",
    });
    expect(
      presetPeriod(
        { ...DEFAULT_FILTERS, preset: "custom", from: "2025-03-01", to: "2025-04-01" },
        today,
      ),
    ).toEqual({ from: "2025-03-01", to: "2025-04-01" });
  });

  it("rolls this month over a year end", () => {
    expect(
      presetPeriod({ ...DEFAULT_FILTERS, preset: "this_month" }, new Date("2026-12-31T23:00:00Z")),
    ).toEqual({ from: "2026-12-01", to: "2027-01-01" });
  });

  it("falls back to this month for an incomplete or inverted custom range", () => {
    const thisMonth = { from: "2026-11-01", to: "2026-12-01" };
    expect(presetPeriod({ ...DEFAULT_FILTERS, preset: "custom" }, today)).toEqual(thisMonth);
    expect(
      presetPeriod(
        { ...DEFAULT_FILTERS, preset: "custom", from: "2025-04-01", to: "2025-03-01" },
        today,
      ),
    ).toEqual(thisMonth);
  });
});

describe("chooseBucket", () => {
  it("buckets a long daily range by month and keeps a short one at its grain", () => {
    expect(chooseBucket("day", { from: "2024-01-01", to: "2026-01-01" })).toBe("month");
    expect(chooseBucket("day", { from: "2026-11-01", to: "2026-12-01" })).toBe("grain");
  });

  it("never re-buckets a monthly dataset", () => {
    expect(chooseBucket("month", { from: "2010-01-01", to: "2026-01-01" })).toBe("grain");
  });
});

describe("blockQuery", () => {
  it("builds the kpi query with its own dataset's category filters", () => {
    expect(
      blockQuery(
        kpiBlock,
        spec,
        { ...DEFAULT_FILTERS, compare: "previous_period", categories: { region: ["north"] } },
        today,
      ),
    ).toEqual({
      dataset: "sales",
      fields: ["amount"],
      period: { from: "2026-11-01", to: "2026-12-01" },
      compare: "previous_period",
      filters: { region: ["north"] },
      groupBy: null,
      mode: "aggregate",
      reported: "latest",
      bucket: "grain",
    });
  });

  // F14: the spec's KPI shows the change against the previous period even when
  // the filter state asks for no comparison; a chart keeps "none".
  it("gives a kpi the previous period by default but not a chart", () => {
    expect(blockQuery(kpiBlock, spec, DEFAULT_FILTERS, today)?.compare).toBe("previous_period");
    expect(blockQuery(chartBlock, spec, DEFAULT_FILTERS, today)?.compare).toBe("none");
    expect(
      blockQuery(kpiBlock, spec, { ...DEFAULT_FILTERS, compare: "same_period_last_year" }, today)
        ?.compare,
    ).toBe("same_period_last_year");
  });

  it("groups a chart by its split category and passes first-reported through", () => {
    const q = blockQuery(chartBlock, spec, { ...DEFAULT_FILTERS, reported: "first" }, today);
    expect(q?.groupBy).toBe("region");
    expect(q?.mode).toBe("aggregate");
    expect(q?.reported).toBe("first");
    expect(q?.fields).toEqual(["amount"]);
  });

  it("pages a table in rows mode with its page size", () => {
    const q = blockQuery(tableBlock, spec, DEFAULT_FILTERS, today);
    expect(q?.mode).toBe("rows");
    expect(q?.fields).toEqual(["amount", "region"]);
    expect(q?.page).toEqual({ offset: 0, limit: 10 });
  });

  it("asks for one row of a note's dataset, enough to carry the note", () => {
    const q = blockQuery(noteBlock, spec, DEFAULT_FILTERS, today);
    expect(q?.mode).toBe("rows");
    expect(q?.fields).toEqual(["amount"]);
    expect(q?.page).toEqual({ offset: 0, limit: 1 });
  });

  it("returns null for blocks that query nothing themselves", () => {
    expect(blockQuery(filterBar, spec, DEFAULT_FILTERS, today)).toBeNull();
    expect(blockQuery(artifactBlock, spec, DEFAULT_FILTERS, today)).toBeNull();
  });

  it("keeps another dataset's category filter off the block", () => {
    const q = blockQuery(
      kpiBlock,
      spec,
      { ...DEFAULT_FILTERS, categories: { team: ["ops"], region: [] } },
      today,
    );
    expect(q?.filters).toEqual({});
  });

  it("re-buckets a long range by month", () => {
    const q = blockQuery(chartBlock, spec, { ...DEFAULT_FILTERS, preset: "last_12_months" }, today);
    expect(q?.bucket).toBe("month");
    expect(q?.period).toEqual({ from: "2025-12-01", to: "2026-12-01" });
  });
});

// F24: aggregate mode refuses non-numeric fields, so the category values come
// from a grouped aggregate over the first numeric field, or from rows.
describe("categoryValuesQuery", () => {
  it("groups the dataset's first numeric field by the category", () => {
    expect(
      categoryValuesQuery(spec, { dataset: "sales", field: "region" }, DEFAULT_FILTERS, today),
    ).toEqual({
      dataset: "sales",
      fields: ["amount"],
      period: { from: "2026-11-01", to: "2026-12-01" },
      compare: "none",
      filters: {},
      groupBy: "region",
      mode: "aggregate",
      reported: "latest",
      bucket: "year",
    });
  });

  it("reads rows of the category when the dataset has no numeric field", () => {
    const q = categoryValuesQuery(
      spec,
      { dataset: "tickets", field: "team" },
      DEFAULT_FILTERS,
      today,
    );
    expect(q?.mode).toBe("rows");
    expect(q?.fields).toEqual(["team"]);
    expect(q?.groupBy).toBeNull();
    expect(q?.page).toEqual({ offset: 0, limit: 200 });
  });

  it("returns null for an unknown dataset", () => {
    expect(
      categoryValuesQuery(spec, { dataset: "nope", field: "x" }, DEFAULT_FILTERS, today),
    ).toBeNull();
  });
});

describe("categoryValues", () => {
  it("collects groups from totals and distinct values from rows, sorted", () => {
    const grouped: QueryResult = {
      series: [],
      totals: [
        { group: "south", values: { amount: 1 } },
        { group: "north", values: { amount: 2 } },
        { group: null, values: { amount: 3 } },
      ],
      compare: null,
      rows: null,
      totalRows: null,
      note: null,
    };
    expect(categoryValues(grouped, "region")).toEqual(["north", "south"]);

    const rows: QueryResult = {
      series: [],
      totals: [],
      compare: null,
      rows: [
        { as_of: "2026-11-02T00:00:00+00:00", team: "ops" },
        { as_of: "2026-11-03T00:00:00+00:00", team: "dev" },
        { as_of: "2026-11-04T00:00:00+00:00", team: "ops" },
        { as_of: "2026-11-05T00:00:00+00:00", team: null },
      ],
      totalRows: 4,
      note: null,
    };
    expect(categoryValues(rows, "team")).toEqual(["dev", "ops"]);
    expect(categoryValues(undefined, "team")).toEqual([]);
  });
});
