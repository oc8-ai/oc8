// Pure pivot of a query's series into recharts rows. No React.
import type { ChartBlock as ChartSpec, DatasetSpec, QueryResult } from "@/lib/boards-api";

import { NO_VALUE } from "./board-format";

export interface ChartSeries {
  /** `s0`, `s1`, ...: safe as a CSS variable name, unlike a group value. */
  key: string;
  label: string;
  field: string;
  group: string | null;
}

export type ChartRow = { asOf: string } & Record<string, string | number | null>;

/** Pivot the series points into recharts rows: one series per field, or per
 *  (field, group) when the block is split by a category. */
export function chartData(
  result: QueryResult,
  block: ChartSpec,
  dataset: DatasetSpec,
): { rows: ChartRow[]; series: ChartSeries[] } {
  const label = (key: string) => dataset.fields.find((f) => f.key === key)?.label ?? key;
  const series: ChartSeries[] = [];
  const keyOf = new Map<string, string>();
  const ensure = (field: string, group: string | null): string => {
    const id = JSON.stringify([field, group]);
    let key = keyOf.get(id);
    if (key === undefined) {
      key = `s${series.length}`;
      keyOf.set(id, key);
      const groupLabel = group ?? NO_VALUE;
      series.push({
        key,
        field,
        group,
        label: !block.split_by
          ? label(field)
          : block.fields.length > 1
            ? `${label(field)} · ${groupLabel}`
            : groupLabel,
      });
    }
    return key;
  };
  if (!block.split_by) for (const f of block.fields) ensure(f, null);

  const rows: ChartRow[] = [];
  const rowAt = new Map<string, ChartRow>();
  for (const point of result.series) {
    let row = rowAt.get(point.asOf);
    if (!row) {
      row = { asOf: point.asOf };
      rowAt.set(point.asOf, row);
      rows.push(row);
    }
    for (const f of block.fields) {
      const key = ensure(f, block.split_by ? point.group : null);
      row[key] = point.values[f] ?? null;
    }
  }
  return { rows, series };
}
