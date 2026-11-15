import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  XAxis,
  YAxis,
} from "recharts";

import { WidgetTitle } from "@/components/dashboard/widget-title";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import type { ChartConfig } from "@/components/ui/chart";
import { useBoardQuery } from "@/lib/boards-api";
import type {
  BoardQuery,
  ChartBlock as ChartSpec,
  DatasetSpec,
  QueryResult,
} from "@/lib/boards-api";
import { useLang, useT } from "@/lib/i18n";

import { BlockMessage, BlockStatus } from "./block-status";
import { formatAsOf, formatCompact, NO_VALUE } from "./board-format";

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

const color = (i: number) => `var(--chart-${(i % 5) + 1})`;

/** A line, bar or area chart of fields over time. */
export function ChartBlock({
  boardId,
  block,
  dataset,
  query,
}: {
  boardId: string;
  block: ChartSpec;
  dataset: DatasetSpec;
  query: BoardQuery | null;
}) {
  const t = useT();
  const { lang } = useLang();
  const { data, error } = useBoardQuery(boardId, query);
  const title = block.title || dataset.label;

  return (
    <div className="flex h-full flex-col">
      <WidgetTitle title={title} />
      <BlockStatus data={data} error={error} t={t} />
      {data && (
        <ChartBody
          result={data}
          block={block}
          dataset={dataset}
          bucket={query?.bucket ?? "grain"}
          lang={lang}
          noData={t("No data for this period", "Keine Daten für diesen Zeitraum")}
          legendLabel={t("Legend", "Legende")}
        />
      )}
    </div>
  );
}

function ChartBody({
  result,
  block,
  dataset,
  bucket,
  lang,
  noData,
  legendLabel,
}: {
  result: QueryResult;
  block: ChartSpec;
  dataset: DatasetSpec;
  bucket: string;
  lang: string;
  noData: string;
  legendLabel: string;
}) {
  const { rows, series } = chartData(result, block, dataset);
  if (rows.length === 0) return <BlockMessage>{noData}</BlockMessage>;

  const config: ChartConfig = Object.fromEntries(
    series.map((s, i) => [s.key, { label: s.label, color: color(i) }]),
  );
  const tick = (v: string) => formatAsOf(v, dataset.grain, lang, bucket);
  const axes = (
    <>
      <CartesianGrid vertical={false} />
      <XAxis
        dataKey="asOf"
        tickLine={false}
        axisLine={false}
        tickFormatter={tick}
        minTickGap={16}
      />
      <YAxis
        tickLine={false}
        axisLine={false}
        width={40}
        tickFormatter={(v: number) => formatCompact(v, lang)}
      />
      <ChartTooltip content={<ChartTooltipContent labelFormatter={(v) => tick(String(v))} />} />
    </>
  );

  return (
    <div className="flex min-w-0 flex-col gap-2 px-3 pb-3">
      <ChartContainer className="aspect-auto h-48 w-full" config={config}>
        {block.type === "bar" ? (
          <BarChart data={rows}>
            {axes}
            {series.map((s) => (
              <Bar key={s.key} dataKey={s.key} fill={`var(--color-${s.key})`} radius={2} />
            ))}
          </BarChart>
        ) : block.type === "area" ? (
          <AreaChart data={rows}>
            {axes}
            {series.map((s) => (
              <Area
                key={s.key}
                dataKey={s.key}
                type="monotone"
                stroke={`var(--color-${s.key})`}
                fill={`var(--color-${s.key})`}
                fillOpacity={0.15}
                strokeWidth={2}
              />
            ))}
          </AreaChart>
        ) : (
          <LineChart data={rows}>
            {axes}
            {series.map((s) => (
              <Line
                key={s.key}
                dataKey={s.key}
                type="monotone"
                stroke={`var(--color-${s.key})`}
                strokeWidth={2}
                dot={false}
              />
            ))}
          </LineChart>
        )}
      </ChartContainer>
      {series.length > 1 && (
        <ul aria-label={legendLabel} className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
          {series.map((s, i) => (
            <li key={s.key} className="flex min-w-0 items-center gap-1.5 text-muted-foreground">
              <span
                aria-hidden
                className="size-2 shrink-0 rounded-[2px]"
                style={{ background: color(i) }}
              />
              <span className="truncate">{s.label}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
