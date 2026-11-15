// Pure filter logic for a dashboard: the filter state a tile keeps, the period
// a preset stands for, and the query each block sends. No React, no I/O.
//
// Dates are ISO `YYYY-MM-DD` in UTC, the calendar the backend aggregates in,
// and every period is half-open: `to` is the first day not in it.
import type {
  BoardBlock,
  BoardQuery,
  BoardSpec,
  CategoryFilterRef,
  DatasetSpec,
  Period,
  QueryResult,
} from "@/lib/boards-api";

export type BoardFilterState = {
  preset: "this_month" | "last_12_months" | "year_to_date" | "custom";
  from?: string;
  to?: string;
  compare: "none" | "previous_period" | "same_period_last_year";
  /** Selected values per category field key; empty or absent = no filter. */
  categories: Record<string, string[]>;
  reported: "latest" | "first";
};

export const DEFAULT_FILTERS: BoardFilterState = {
  preset: "this_month",
  compare: "none",
  categories: {},
  reported: "latest",
};

const DAY_MS = 86_400_000;
/** Above this many points at the dataset's grain, a series goes by month. */
const MAX_GRAIN_POINTS = 92;
/** Distinct category values read when a dataset has no numeric field. */
const CATEGORY_ROWS_LIMIT = 200;
const NUMERIC = new Set(["number", "money", "percent"]);

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function utcDay(year: number, month: number, day: number): string {
  return isoDay(new Date(Date.UTC(year, month, day)));
}

/** `iso` moved by `days` calendar days. */
export function addDays(iso: string, days: number): string {
  return isoDay(new Date(Date.parse(`${iso}T00:00:00Z`) + days * DAY_MS));
}

function validRange(from: string | undefined, to: string | undefined): from is string {
  return (
    !!from && !!to && !Number.isNaN(Date.parse(from)) && !Number.isNaN(Date.parse(to)) && from < to
  );
}

/** A custom range with a new start. A start on or after the end moves the
 *  end to the day after it, so the range never inverts. */
export function withCustomFrom(state: BoardFilterState, from: string): BoardFilterState {
  const to = state.to && state.to > from ? state.to : addDays(from, 1);
  return { ...state, from, to };
}

/** A custom range with a new last day (inclusive, as a date picker shows it;
 *  stored exclusive). An end before the start pulls the start back to it. */
export function withCustomLastDay(state: BoardFilterState, lastDay: string): BoardFilterState {
  const to = addDays(lastDay, 1);
  const from = state.from && state.from < to ? state.from : addDays(to, -1);
  return { ...state, from, to };
}

export function presetPeriod(state: BoardFilterState, today: Date): Period {
  const y = today.getUTCFullYear();
  const m = today.getUTCMonth();
  switch (state.preset) {
    case "last_12_months":
      return { from: utcDay(y, m - 11, 1), to: utcDay(y, m + 1, 1) };
    case "year_to_date":
      return { from: utcDay(y, 0, 1), to: utcDay(y, m, today.getUTCDate() + 1) };
    case "custom":
      if (validRange(state.from, state.to)) return { from: state.from, to: state.to as string };
      break;
    case "this_month":
      break;
  }
  return { from: utcDay(y, m, 1), to: utcDay(y, m + 1, 1) };
}

/** Keep the dataset's own grain unless that would draw too many points. */
export function chooseBucket(grain: string, period: Period): "grain" | "month" {
  const days = (Date.parse(period.to) - Date.parse(period.from)) / DAY_MS;
  const points = grain === "month" ? 0 : grain === "week" ? days / 7 : days;
  return points > MAX_GRAIN_POINTS ? "month" : "grain";
}

function datasetOf(spec: BoardSpec, key: string): DatasetSpec | undefined {
  return spec.datasets.find((d) => d.key === key);
}

/** The filter bar's category refs, if the board has one. */
export function categoryRefs(spec: BoardSpec): CategoryFilterRef[] {
  const bar = spec.blocks.find((b) => b.type === "filter_bar");
  return bar?.type === "filter_bar" ? (bar.category_filters ?? []) : [];
}

/** The selected categories that belong to `dataset`: the filter bar names the
 *  field for that dataset, and something is selected. Another dataset's
 *  category never reaches this block's query. */
function filtersFor(
  spec: BoardSpec,
  dataset: string,
  state: BoardFilterState,
): Record<string, string[]> {
  const own = new Set(
    categoryRefs(spec)
      .filter((r) => r.dataset === dataset)
      .map((r) => r.field),
  );
  const out: Record<string, string[]> = {};
  for (const [field, values] of Object.entries(state.categories)) {
    if (own.has(field) && values.length > 0) out[field] = values;
  }
  return out;
}

export function blockQuery(
  block: BoardBlock,
  spec: BoardSpec,
  state: BoardFilterState,
  today: Date,
): BoardQuery | null {
  if (block.type === "filter_bar" || block.type === "artifact") return null;
  const ds = datasetOf(spec, block.dataset);
  if (!ds) return null;
  const period = presetPeriod(state, today);
  const base = {
    dataset: block.dataset,
    period,
    filters: filtersFor(spec, block.dataset, state),
    reported: state.reported,
  };
  switch (block.type) {
    case "kpi":
      return {
        ...base,
        fields: [block.field],
        // The KPI always shows a change (spec §6.3): the previous period
        // unless the filter bar picked another comparison.
        compare: state.compare === "none" ? "previous_period" : state.compare,
        groupBy: null,
        mode: "aggregate",
        bucket: chooseBucket(ds.grain, period),
      };
    case "line":
    case "bar":
    case "area":
      return {
        ...base,
        fields: block.fields,
        // Charts draw the current period only, so they ask for no comparison.
        compare: "none",
        groupBy: block.split_by ?? null,
        mode: "aggregate",
        bucket: chooseBucket(ds.grain, period),
      };
    case "table":
      return {
        ...base,
        fields: block.fields,
        compare: "none",
        groupBy: null,
        mode: "rows",
        bucket: "grain",
        page: { offset: 0, limit: block.page_size ?? 25 },
      };
    case "note":
      // The note rides along with any query; one row of any field is the
      // cheapest one rows mode accepts.
      return {
        ...base,
        fields: [ds.fields[0].key],
        compare: "none",
        groupBy: null,
        mode: "rows",
        bucket: "grain",
        page: { offset: 0, limit: 1 },
      };
  }
}

/** The query whose result lists a category's values in the current period.
 *  Aggregate mode only takes numeric fields, so it groups the dataset's first
 *  numeric field by the category; without one, it reads rows of the category. */
export function categoryValuesQuery(
  spec: BoardSpec,
  ref: CategoryFilterRef,
  state: BoardFilterState,
  today: Date,
): BoardQuery | null {
  const ds = datasetOf(spec, ref.dataset);
  if (!ds) return null;
  const base = {
    dataset: ref.dataset,
    period: presetPeriod(state, today),
    compare: "none" as const,
    filters: {},
    reported: state.reported,
  };
  const numeric = ds.fields.find((f) => NUMERIC.has(f.type));
  if (numeric) {
    return {
      ...base,
      fields: [numeric.key],
      groupBy: ref.field,
      mode: "aggregate",
      // Only the grouped totals are read; one bucket keeps the series small.
      bucket: "year",
    };
  }
  return {
    ...base,
    fields: [ref.field],
    groupBy: null,
    mode: "rows",
    bucket: "grain",
    page: { offset: 0, limit: CATEGORY_ROWS_LIMIT },
  };
}

/** The distinct values a `categoryValuesQuery` result names, sorted. */
export function categoryValues(result: QueryResult | undefined, field: string): string[] {
  if (!result) return [];
  const found = new Set<string>();
  if (result.rows) {
    for (const row of result.rows) {
      const v = row[field];
      if (typeof v === "string") found.add(v);
    }
  } else {
    for (const t of result.totals) if (t.group !== null) found.add(t.group);
  }
  return [...found].sort((a, b) => a.localeCompare(b));
}
