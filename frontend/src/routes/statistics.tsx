import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import {
  BarChart,
  Bar,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  XAxis,
} from "recharts";
import { Panel } from "@/components/app-shell";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { useAgents, useDepartments, useTenantKpis } from "@/lib/hooks";
import { formatMs } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/statistics")({
  component: StatisticsPage,
});

type ChartType = "bar" | "pie" | "line";
type GroupBy = "agent" | "department" | "day" | "week" | "month";

/** `AgentRun.state`'s own CHECK-constrained vocabulary (backend
 *  models/run.py) -- `GET /kpis`'s `status` narrows to exactly one of these,
 *  so the picker offers exactly these and nothing invented. */
const RUN_STATES = [
  "queued",
  "running",
  "waiting_for_input",
  "waiting_for_approval",
  "failed",
  "done",
  "interrupted",
] as const;

const GROUP_BY_VALUES: GroupBy[] = ["agent", "department", "day", "week", "month"];

/** `ApiError` (lib/api.ts) carries the HTTP status on the thrown Error; React
 *  Query hands that same object back as `error`. Read defensively -- a
 *  network failure throws a plain `Error` with no `status` at all. */
function statusOf(error: unknown): number | undefined {
  if (typeof error === "object" && error !== null && "status" in error) {
    const s = (error as { status?: unknown }).status;
    if (typeof s === "number") return s;
  }
  return undefined;
}

/** `<select>`'s empty option means "no filter", which has to reach
 *  `useTenantKpis` as `undefined` -- `buildQuery` drops `undefined` but would
 *  happily put an empty string on the wire. */
function orUndefined(value: string): string | undefined {
  return value === "" ? undefined : value;
}

const FIELD_CLASS =
  "rounded-md border border-border bg-panel px-3 py-1.5 text-sm outline-none focus:border-primary/50";

export function StatisticsPage() {
  const t = useT();
  const [agentId, setAgentId] = useState<string | undefined>(undefined);
  const [departmentId, setDepartmentId] = useState<string | undefined>(undefined);
  const [dateFrom, setDateFrom] = useState<string | undefined>(undefined);
  const [dateTo, setDateTo] = useState<string | undefined>(undefined);
  const [status, setStatus] = useState<string | undefined>(undefined);
  // "agent", deliberately, as the first-load grouping. The backend caps a
  // request at MAX_BUCKETS=100 buckets and MAX_GROUPS=100 groups and REJECTS
  // (422) rather than truncating, so the default has to be one that cannot
  // trip a cap before the user has picked anything: with no date bounds a
  // date-bucketed grouping is the risky one, while enumerating agents is
  // bounded by how many agents the tenant has. A tenant with more than 100
  // live agents still gets the 422, which is why the explainer below exists.
  const [groupBy, setGroupBy] = useState<GroupBy>("agent");
  const [chartType, setChartType] = useState<ChartType>("bar");

  const kpis = useTenantKpis({ agentId, departmentId, dateFrom, dateTo, status, groupBy });
  // Filter dropdowns over every agent/department, not a paginated list view --
  // same call shape `routes/activity.tsx` already uses for its own picker.
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const agents = agentsPage?.items ?? [];
  const departments = departmentsPage?.items ?? [];

  const rows = kpis.data && "rows" in kpis.data ? kpis.data.rows : [];

  // `groupKey` is a raw uuid for the two enumerating groupings and an ISO date
  // for the three bucketed ones. Resolve the former to a name; show the latter
  // as-is. An id with no matching row (a filter list that has not landed yet,
  // or a row soft-deleted since) falls back to the id rather than to a blank.
  const labelFor = (groupKey: string): string => {
    if (groupBy === "agent") return agents.find((a) => a.id === groupKey)?.name ?? groupKey;
    if (groupBy === "department")
      return departments.find((d) => d.id === groupKey)?.name ?? groupKey;
    return groupKey;
  };

  // `groupKey` rides along purely as a stable React key for the pie's <Cell>s:
  // two agents may legitimately share a display name, and keying the cells on
  // the label alone would then collide.
  const chartRows = rows.map((r) => ({
    groupKey: r.groupKey,
    label: labelFor(r.groupKey),
    runCount: r.runCount,
  }));
  const chartConfig = { runCount: { label: t("Runs", "Runs"), color: "var(--primary)" } };

  const errorStatus = kpis.isError ? statusOf(kpis.error) : undefined;
  const capRejected = errorStatus === 422;

  const groupByLabel = (g: GroupBy) =>
    ({
      agent: t("Agent", "Agent"),
      department: t("Department", "Abteilung"),
      day: t("Day", "Tag"),
      week: t("Week", "Woche"),
      month: t("Month", "Monat"),
    })[g];

  const chartTypeLabel = (c: ChartType) =>
    ({ bar: t("Bar", "Balken"), pie: t("Pie", "Kreis"), line: t("Line", "Linie") })[c];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs uppercase tracking-wider text-muted-foreground">
          {t("Agent", "Agent")}
          <select
            aria-label={t("Agent", "Agent")}
            value={agentId ?? ""}
            onChange={(e) => setAgentId(orUndefined(e.target.value))}
            className={FIELD_CLASS}
          >
            <option value="">{t("All agents", "Alle Agenten")}</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs uppercase tracking-wider text-muted-foreground">
          {t("Department", "Abteilung")}
          <select
            aria-label={t("Department", "Abteilung")}
            value={departmentId ?? ""}
            onChange={(e) => setDepartmentId(orUndefined(e.target.value))}
            className={FIELD_CLASS}
          >
            <option value="">{t("All departments", "Alle Abteilungen")}</option>
            {departments.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs uppercase tracking-wider text-muted-foreground">
          {t("From", "Von")}
          <input
            type="date"
            aria-label={t("From", "Von")}
            value={dateFrom ?? ""}
            onChange={(e) => setDateFrom(orUndefined(e.target.value))}
            className={FIELD_CLASS}
          />
        </label>

        <label className="flex flex-col gap-1 text-xs uppercase tracking-wider text-muted-foreground">
          {t("To", "Bis")}
          <input
            type="date"
            aria-label={t("To", "Bis")}
            value={dateTo ?? ""}
            onChange={(e) => setDateTo(orUndefined(e.target.value))}
            className={FIELD_CLASS}
          />
        </label>

        <label className="flex flex-col gap-1 text-xs uppercase tracking-wider text-muted-foreground">
          {t("Status", "Status")}
          <select
            aria-label={t("Status", "Status")}
            value={status ?? ""}
            onChange={(e) => setStatus(orUndefined(e.target.value))}
            className={FIELD_CLASS}
          >
            <option value="">{t("Any status", "Beliebiger Status")}</option>
            {RUN_STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs uppercase tracking-wider text-muted-foreground">
          {t("Group by", "Gruppieren nach")}
          <select
            aria-label={t("Group by", "Gruppieren nach")}
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as GroupBy)}
            className={FIELD_CLASS}
          >
            {GROUP_BY_VALUES.map((g) => (
              <option key={g} value={g}>
                {groupByLabel(g)}
              </option>
            ))}
          </select>
        </label>

        <div className="ml-auto flex gap-1">
          {(["bar", "line", "pie"] as ChartType[]).map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setChartType(c)}
              className={cn(
                "rounded-md border px-2.5 py-1 text-xs",
                chartType === c
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-panel text-muted-foreground hover:text-foreground",
              )}
            >
              {chartTypeLabel(c)}
            </button>
          ))}
        </div>
      </div>

      {kpis.isError ? (
        // The backend rejects an over-wide request outright (MAX_BUCKETS /
        // MAX_GROUPS) rather than returning a quietly-truncated series, so a
        // 422 here is a filter problem the reader can fix, not a fault. Say
        // which knobs to turn; never auto-narrow behind his back.
        <Panel className="p-5">
          <p className="text-sm text-muted-foreground">
            {capRejected
              ? t(
                  "This filter produces too many results. Narrow the date range, choose a coarser grouping, or filter by an agent or department.",
                  "Dieser Filter liefert zu viele Ergebnisse. Grenze den Zeitraum ein, wähle eine gröbere Gruppierung oder filtere nach Agent oder Abteilung.",
                )
              : t("Statistics could not be loaded.", "Statistiken konnten nicht geladen werden.")}
          </p>
        </Panel>
      ) : (
        <>
          <Panel className="p-5">
            {chartType === "bar" && (
              <ChartContainer className="aspect-auto h-72" config={chartConfig} data-testid="chart-bar">
                <BarChart data={chartRows}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="label" tickLine={false} axisLine={false} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Bar dataKey="runCount" fill="var(--color-runCount)" radius={4} />
                </BarChart>
              </ChartContainer>
            )}
            {chartType === "line" && (
              <ChartContainer className="aspect-auto h-72" config={chartConfig} data-testid="chart-line">
                <LineChart data={chartRows}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="label" tickLine={false} axisLine={false} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Line
                    dataKey="runCount"
                    stroke="var(--color-runCount)"
                    strokeWidth={2}
                    dot={false}
                  />
                </LineChart>
              </ChartContainer>
            )}
            {chartType === "pie" && (
              <ChartContainer
                className="aspect-auto h-72"
                config={{ runCount: { label: t("Runs", "Runs") } }}
                data-testid="chart-pie"
              >
                <PieChart>
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Pie data={chartRows} dataKey="runCount" nameKey="label" label>
                    {chartRows.map((r, i) => (
                      // `--chart-1..5` already exist in styles.css (both
                      // themes); cycled, so a 6th group repeats the first
                      // colour rather than resolving to an undefined var.
                      <Cell key={r.groupKey} fill={`var(--chart-${(i % 5) + 1})`} />
                    ))}
                  </Pie>
                </PieChart>
              </ChartContainer>
            )}
          </Panel>

          <Panel className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <th className="px-4 py-2 font-medium">{groupByLabel(groupBy)}</th>
                  <th className="px-4 py-2 text-right font-medium">{t("Runs", "Runs")}</th>
                  <th className="px-4 py-2 text-right font-medium">
                    {t("Total duration", "Gesamtdauer")}
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    {t("Execution", "Ausführung")}
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    {t("Approval wait", "Approval-Wartezeit")}
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    {t("Response time", "Antwortzeit")}
                  </th>
                  <th className="px-4 py-2 text-right font-medium">
                    {t("Avg. tool call", "Ø Tool-Aufruf")}
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((r) => (
                  <tr key={r.groupKey} data-testid="kpi-row">
                    <td className="px-4 py-2">{labelFor(r.groupKey)}</td>
                    <td className="px-4 py-2 text-right font-mono">{r.runCount}</td>
                    <td className="px-4 py-2 text-right font-mono">
                      {formatMs(r.totalDurationMs)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono">
                      {formatMs(r.executionDurationMs)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono">{formatMs(r.approvalWaitMs)}</td>
                    <td className="px-4 py-2 text-right font-mono">{formatMs(r.responseTimeMs)}</td>
                    <td className="px-4 py-2 text-right font-mono">
                      {formatMs(r.avgToolCallDurationMs)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 && (
              // "Loading" and "nothing here" are different statements, and
              // saying the second one while the first is true is a lie the
              // reader acts on -- he re-picks a filter that was fine.
              <p className="px-4 py-6 text-sm text-muted-foreground">
                {kpis.isPending
                  ? t("Loading…", "Wird geladen…")
                  : t("No data for this filter.", "Keine Daten für diesen Filter.")}
              </p>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
