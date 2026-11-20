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
import { formatAsOf, formatCompact, formatValue } from "./board-format";
import { chartData } from "./chart-data";

// The theme's chart tokens, in their fixed order: a series keeps its colour
// as long as it keeps its place.
const color = (i: number) => `var(--chart-${(i % 5) + 1})`;
// Room for the first and last tick labels, none wasted at the top.
const CHART_MARGIN = { top: 4, right: 8, bottom: 0, left: 0 };

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
  // Each value formatted as its field (currency, percent), not as a bare number.
  const tooltipRow = (value: unknown, name: unknown) => {
    const i = series.findIndex((s) => s.key === name);
    const s = series[i];
    if (!s) return null;
    const field = dataset.fields.find((f) => f.key === s.field);
    return (
      <>
        <span
          aria-hidden
          className="size-2.5 shrink-0 rounded-[2px]"
          style={{ background: color(i) }}
        />
        <div className="flex flex-1 items-center justify-between gap-2 leading-none">
          <span className="text-muted-foreground">{s.label}</span>
          <span className="font-mono font-medium tabular-nums text-foreground">
            {formatValue(value, field, lang)}
          </span>
        </div>
      </>
    );
  };
  const axes = (
    <>
      {/* Recessive: hairline horizontal grid, no axis lines, small muted ticks. */}
      <CartesianGrid vertical={false} />
      <XAxis
        dataKey="asOf"
        tickLine={false}
        axisLine={false}
        tickFormatter={tick}
        tick={{ fontSize: 11 }}
        tickMargin={6}
        minTickGap={24}
      />
      <YAxis
        tickLine={false}
        axisLine={false}
        width={44}
        tick={{ fontSize: 11 }}
        tickFormatter={(v: number) => formatCompact(v, lang)}
      />
      <ChartTooltip
        cursor={block.type === "bar" ? undefined : { strokeWidth: 1 }}
        content={
          <ChartTooltipContent labelFormatter={(v) => tick(String(v))} formatter={tooltipRow} />
        }
      />
    </>
  );

  return (
    <div className="flex min-w-0 flex-col gap-2 px-3 pb-3">
      <ChartContainer className="aspect-auto h-48 w-full" config={config}>
        {block.type === "bar" ? (
          <BarChart data={rows} barGap={2} margin={CHART_MARGIN}>
            {axes}
            {series.map((s) => (
              // Thin bars with rounded data ends, square at the baseline.
              <Bar
                key={s.key}
                dataKey={s.key}
                fill={`var(--color-${s.key})`}
                radius={[4, 4, 0, 0]}
                maxBarSize={24}
              />
            ))}
          </BarChart>
        ) : block.type === "area" ? (
          <AreaChart data={rows} margin={CHART_MARGIN}>
            {axes}
            {series.map((s) => (
              <Area
                key={s.key}
                dataKey={s.key}
                type="monotone"
                stroke={`var(--color-${s.key})`}
                fill={`var(--color-${s.key})`}
                fillOpacity={0.1}
                strokeWidth={2}
                activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--panel)" }}
              />
            ))}
          </AreaChart>
        ) : (
          <LineChart data={rows} margin={CHART_MARGIN}>
            {axes}
            {series.map((s) => (
              <Line
                key={s.key}
                dataKey={s.key}
                type="monotone"
                stroke={`var(--color-${s.key})`}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--panel)" }}
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
