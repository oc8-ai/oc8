// Renders `bar_chart` and `line_chart` (oc8.agent.components.ChartProps --
// one props shape shared by both catalog entries, see backend/agent/
// components.py). Hand-rolled SVG rather than a charting dependency: the
// data here is always small (<=100 points, <=10 series -- backend-enforced),
// so a full charting library would be a lot of bundle for very little.

import type { ReactNode } from "react";

const SERIES_COLORS = [
  "var(--primary)",
  "var(--status-warning, #eab308)",
  "var(--status-error, #ef4444)",
  "var(--status-running, #22c55e)",
  "#8b5cf6",
  "#06b6d4",
];

interface ChartSeries {
  name: string;
  values: number[];
}

function asSeries(v: unknown): ChartSeries[] {
  if (!Array.isArray(v)) return [];
  return v.filter(
    (s): s is ChartSeries =>
      typeof s === "object" &&
      s !== null &&
      typeof (s as ChartSeries).name === "string" &&
      Array.isArray((s as ChartSeries).values),
  );
}

function asLabels(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((l): l is string => typeof l === "string");
}

const WIDTH = 560;
const HEIGHT = 200;
const PAD_LEFT = 36;
const PAD_BOTTOM = 24;
const PAD_TOP = 12;
const PAD_RIGHT = 12;

function computeChartGeometry(labels: string[], series: ChartSeries[]) {
  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const allValues = series.flatMap((s) => s.values);
  const max = allValues.length > 0 ? Math.max(...allValues, 0) : 0;
  const min = allValues.length > 0 ? Math.min(...allValues, 0) : 0;
  const range = max - min || 1;
  const y = (value: number) => PAD_TOP + plotH - ((value - min) / range) * plotH;
  const stepX = labels.length > 1 ? plotW / (labels.length - 1) : plotW;
  const x = (i: number) => PAD_LEFT + (labels.length > 1 ? i * stepX : plotW / 2);
  return { plotW, plotH, max, min, y, x, stepX };
}

function ChartLegend({ series }: { series: ChartSeries[] }) {
  if (series.length <= 1) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
      {series.map((s, i) => (
        <span key={s.name} className="inline-flex items-center gap-1.5">
          <span
            className="h-2 w-2 rounded-full"
            style={{ backgroundColor: SERIES_COLORS[i % SERIES_COLORS.length] }}
          />
          {s.name}
        </span>
      ))}
    </div>
  );
}

function ChartShell({
  title,
  labels,
  series,
  children,
}: {
  title: string;
  labels: string[];
  series: ChartSeries[];
  children: (geo: ReturnType<typeof computeChartGeometry>) => ReactNode;
}) {
  const geo = computeChartGeometry(labels, series);
  const yTicks = [geo.min, (geo.min + geo.max) / 2, geo.max];

  return (
    <section className="rounded-lg border border-border bg-background/40 p-3 text-sm">
      {title && <div className="mb-2 font-medium">{title}</div>}
      {labels.length === 0 || series.length === 0 ? (
        <div className="py-6 text-center text-xs text-muted-foreground">No data.</div>
      ) : (
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full" role="img" aria-label={title}>
          {yTicks.map((v, i) => (
            <g key={i}>
              <line
                x1={PAD_LEFT}
                x2={WIDTH - PAD_RIGHT}
                y1={geo.y(v)}
                y2={geo.y(v)}
                stroke="var(--border)"
                strokeWidth={1}
              />
              <text
                x={PAD_LEFT - 6}
                y={geo.y(v) + 3}
                textAnchor="end"
                fontSize={9}
                fill="var(--muted-foreground)"
              >
                {Math.round(v * 100) / 100}
              </text>
            </g>
          ))}
          {children(geo)}
          {labels.map((label, i) => (
            <text
              key={i}
              x={geo.x(i)}
              y={HEIGHT - 4}
              textAnchor="middle"
              fontSize={9}
              fill="var(--muted-foreground)"
            >
              {label}
            </text>
          ))}
        </svg>
      )}
      <ChartLegend series={series} />
    </section>
  );
}

export function BarChart({ props }: { props: Record<string, unknown> }) {
  const title = typeof props.title === "string" ? props.title : "";
  const labels = asLabels(props.labels);
  const series = asSeries(props.series);

  return (
    <ChartShell title={title} labels={labels} series={series}>
      {(geo) => {
        const groupWidth = geo.stepX * 0.6;
        const barWidth = groupWidth / Math.max(series.length, 1);
        return (
          <>
            {series.map((s, si) => (
              <g key={s.name}>
                {s.values.map((v, i) => {
                  const barX = geo.x(i) - groupWidth / 2 + si * barWidth;
                  const barY = Math.min(geo.y(v), geo.y(0));
                  const barH = Math.abs(geo.y(v) - geo.y(0));
                  return (
                    <rect
                      key={i}
                      x={barX}
                      y={barY}
                      width={Math.max(barWidth - 2, 1)}
                      height={Math.max(barH, 0.5)}
                      fill={SERIES_COLORS[si % SERIES_COLORS.length]}
                      rx={1.5}
                    />
                  );
                })}
              </g>
            ))}
          </>
        );
      }}
    </ChartShell>
  );
}

export function LineChart({ props }: { props: Record<string, unknown> }) {
  const title = typeof props.title === "string" ? props.title : "";
  const labels = asLabels(props.labels);
  const series = asSeries(props.series);

  return (
    <ChartShell title={title} labels={labels} series={series}>
      {(geo) => (
        <>
          {series.map((s, si) => {
            const points = s.values.map((v, i) => `${geo.x(i)},${geo.y(v)}`).join(" ");
            return (
              <g key={s.name}>
                <polyline
                  points={points}
                  fill="none"
                  stroke={SERIES_COLORS[si % SERIES_COLORS.length]}
                  strokeWidth={2}
                />
                {s.values.map((v, i) => (
                  <circle
                    key={i}
                    cx={geo.x(i)}
                    cy={geo.y(v)}
                    r={2.5}
                    fill={SERIES_COLORS[si % SERIES_COLORS.length]}
                  />
                ))}
              </g>
            );
          })}
        </>
      )}
    </ChartShell>
  );
}
