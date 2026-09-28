import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import {
  Building2,
  Bell,
  Ban,
  Cpu,
  Coins,
  Gauge,
  ShieldAlert,
  Download,
  Plus,
  RefreshCw,
  Trash2,
  Sparkles,
  Users,
  Wallet,
} from "lucide-react";
import { Panel } from "@/components/app-shell";
import type { Agent, Department } from "@/lib/mock-data";
import {
  convert,
  currentSpendForLimit,
  formatMoney,
  formatTokens,
  usePricing,
  useLimits,
  FX_USD_EUR,
  type CostLimit,
  type LimitScope,
  type LimitPeriod,
  type LimitAction,
} from "@/lib/costs";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import {
  useAgents,
  useBudgets,
  useBudgetStatus,
  useDepartments,
  useSecrets,
  useSetBudget,
  useUsage,
  type BudgetDTO,
  type UsageDTO,
} from "@/lib/hooks";
import { useReconciliation, useRefreshReconciliation } from "@/lib/reconciliation-hooks";
import { downloadUsageExport } from "@/lib/usage-export";

export const Route = createFileRoute("/costs")({
  component: CostsPage,
});

type Tab = "overview" | "agents" | "departments" | "limits";
type Currency = "EUR" | "USD";

// Real per-agent / per-department spend, derived from the platform's metered
// /usage endpoint (TokenUsageRecord aggregates) joined against the real
// agent/department lists for display names. No client-side pricing model
// involved — providerCostMicros is what the provider actually billed.
interface AgentSpendRow {
  id: string; // raw `group` from the DTO (agent id, or "unassigned")
  name: string;
  role: string | null;
  llm: string | null;
  provider: string | null;
  avatarColor: string;
  departmentId: string | null;
  departmentName: string | null;
  tokensIn: number;
  tokensOut: number;
  costUSD: number;
}

interface DeptSpendRow {
  id: string; // raw `group` from the DTO (department id, or "unassigned")
  name: string;
  accent: string;
  tokensIn: number;
  tokensOut: number;
  costUSD: number;
  savedCostUSD: number;
}

function buildAgentRows(
  usage: UsageDTO[] | undefined,
  agentList: Agent[] | undefined,
  deptList: Department[] | undefined,
  unassignedLabel: string,
): AgentSpendRow[] {
  return (usage ?? []).map((r) => {
    const agent = agentList?.find((a) => a.id === r.group);
    const dept = agent?.departmentId
      ? deptList?.find((d) => d.id === agent.departmentId)
      : undefined;
    const isUnassigned = !r.group || r.group === "unassigned";
    return {
      id: r.group,
      name: agent?.name ?? (isUnassigned ? unassignedLabel : r.group),
      role: agent?.role ?? null,
      llm: agent?.llm ?? null,
      provider: agent?.provider ?? null,
      avatarColor: agent?.avatarColor ?? "var(--muted-foreground)",
      departmentId: agent?.departmentId ?? null,
      departmentName: dept?.name ?? null,
      tokensIn: r.tokensIn,
      tokensOut: r.tokensOut,
      costUSD: r.providerCostMicros / 1_000_000,
    };
  });
}

export function buildDeptRows(
  usage: UsageDTO[] | undefined,
  deptList: Department[] | undefined,
  unassignedLabel: string,
): DeptSpendRow[] {
  return (usage ?? []).map((r) => {
    const dept = deptList?.find((d) => d.id === r.group);
    const isUnassigned = !r.group || r.group === "unassigned";
    return {
      id: r.group,
      name: dept?.name ?? (isUnassigned ? unassignedLabel : r.group),
      accent: dept?.accent ?? "var(--muted-foreground)",
      tokensIn: r.tokensIn,
      tokensOut: r.tokensOut,
      costUSD: r.providerCostMicros / 1_000_000,
      savedCostUSD: r.savedCostMicros / 1_000_000,
    };
  });
}

function CostsPage() {
  const t = useT();
  const [tab, setTab] = useState<Tab>("overview");
  const [currency, setCurrency] = useState<Currency>("EUR");
  // Cost-breakdown aggregation over every agent/department, not a paginated
  // list view.
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const agentsData = agentsPage?.items;
  const departmentsData = departmentsPage?.items;

  // Real recorded usage from the backend (TokenUsageRecord aggregates), grouped
  // by agent and by department. This is the ground truth the platform actually
  // metered — providerCostMicros is USD·1e6. No client-side pricing model is
  // involved in these figures (that model lives on in the Limits tab only, and
  // is labelled there as an estimate).
  const { data: agentUsage } = useUsage("agent");
  const { data: deptUsage } = useUsage("department");

  const unassignedLabel = t("Unassigned", "Nicht zugewiesen");

  const agentRows = useMemo(
    () => buildAgentRows(agentUsage, agentsData, departmentsData, unassignedLabel),
    [agentUsage, agentsData, departmentsData, unassignedLabel],
  );
  const deptRows = useMemo(
    () => buildDeptRows(deptUsage, departmentsData, unassignedLabel),
    [deptUsage, departmentsData, unassignedLabel],
  );

  const totals = useMemo(() => {
    const totalUSD = agentRows.reduce((s, r) => s + r.costUSD, 0);
    const inTok = agentRows.reduce((s, r) => s + r.tokensIn, 0);
    const outTok = agentRows.reduce((s, r) => s + r.tokensOut, 0);
    return { totalUSD, inTok, outTok };
  }, [agentRows]);

  const recorded = useMemo(() => {
    const rows = deptUsage ?? [];
    const inTok = rows.reduce((s, r) => s + r.tokensIn, 0);
    const outTok = rows.reduce((s, r) => s + r.tokensOut, 0);
    const costUSD = rows.reduce((s, r) => s + r.providerCostMicros, 0) / 1_000_000;
    return { inTok, outTok, costUSD, hasData: inTok + outTok > 0 };
  }, [deptUsage]);

  const tabs: { id: Tab; label: string; icon: typeof Coins }[] = [
    { id: "overview", label: t("Overview", "Übersicht"), icon: Coins },
    { id: "agents", label: t("By Agent", "Pro Agent"), icon: Users },
    { id: "departments", label: t("By Department", "Pro Abteilung"), icon: Building2 },
    { id: "limits", label: t("Limits", "Limits"), icon: ShieldAlert },
  ];

  // Real GET /usage/export download -- the success toast fires only once the
  // fetch has actually resolved and the browser has been handed a file, not
  // optimistically on click (that was this button's original defect: it
  // reported success and downloaded nothing).
  const onExport = async () => {
    try {
      await downloadUsageExport({}, "csv");
      toast.success(t("Report exported", "Bericht exportiert"), {
        description: t("CSV downloaded to your device.", "CSV auf Ihr Gerät geladen."),
      });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t("Export failed", "Export fehlgeschlagen"));
    }
  };

  return (
    <div className="space-y-6">
      <Panel className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
              <Wallet className="h-4 w-4" />
            </div>
            <div>
              <div className="font-serif text-lg leading-tight">
                {t("Cost Center", "Kostencenter")}
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {t(
                  "Token spend across agents and departments — metered by the platform.",
                  "Token-Verbrauch über Agenten und Abteilungen — vom System gemessen.",
                )}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="inline-flex rounded-md border border-border bg-background/40 p-0.5 text-xs">
              {(["EUR", "USD"] as Currency[]).map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setCurrency(c)}
                  className={cn(
                    "rounded px-2.5 py-1 font-medium transition",
                    currency === c
                      ? "bg-primary/20 text-primary"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {c === "EUR" ? "€ EUR" : "$ USD"}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={onExport}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-3 py-2 text-xs text-muted-foreground hover:text-foreground"
            >
              <Download className="h-3.5 w-3.5" />
              {t("Export CSV", "CSV exportieren")}
            </button>
          </div>
        </div>

        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Metric
            label={t("Total spend", "Gesamtausgaben")}
            value={formatMoney(convert(totals.totalUSD, currency), currency)}
            hint={t("recorded to date", "bisher erfasst")}
            highlight
          />
          <Metric label={t("Input tokens", "Input-Token")} value={formatTokens(totals.inTok)} />
          <Metric label={t("Output tokens", "Output-Token")} value={formatTokens(totals.outTok)} />
          <Metric
            label={t("Total tokens", "Token gesamt")}
            value={formatTokens(totals.inTok + totals.outTok)}
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 rounded-md border border-border bg-background/30 px-3 py-2 text-xs">
          <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            {t("Recorded (live)", "Aufgezeichnet (live)")}
          </span>
          {recorded.hasData ? (
            <>
              <span className="text-muted-foreground">
                {t("Cost", "Kosten")}:{" "}
                <span className="font-mono text-foreground">
                  {formatMoney(convert(recorded.costUSD, currency), currency)}
                </span>
              </span>
              <span className="text-muted-foreground">
                {t("Input", "Input")}:{" "}
                <span className="font-mono text-foreground">{formatTokens(recorded.inTok)}</span>
              </span>
              <span className="text-muted-foreground">
                {t("Output", "Output")}:{" "}
                <span className="font-mono text-foreground">{formatTokens(recorded.outTok)}</span>
              </span>
              <span className="text-[10px] text-muted-foreground">
                {t("metered by the platform", "vom System gemessen")}
              </span>
            </>
          ) : (
            <span className="text-muted-foreground">
              {t(
                "No usage metered yet for this tenant.",
                "Für diesen Tenant wurde noch kein Verbrauch gemessen.",
              )}
            </span>
          )}
        </div>

        <div className="mt-5 flex flex-wrap gap-1 border-b border-border">
          {tabs.map((tb) => {
            const Icon = tb.icon;
            const active = tab === tb.id;
            return (
              <button
                key={tb.id}
                type="button"
                onClick={() => setTab(tb.id)}
                className={cn(
                  "-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition",
                  active
                    ? "border-primary text-primary"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon className="h-3.5 w-3.5" />
                {tb.label}
              </button>
            );
          })}
        </div>
      </Panel>

      {tab === "overview" && (
        <OverviewTab agentRows={agentRows} deptRows={deptRows} currency={currency} />
      )}
      {tab === "agents" && <AgentsTab agentRows={agentRows} currency={currency} />}
      {tab === "departments" && (
        <DeptTab deptRows={deptRows} agentRows={agentRows} currency={currency} />
      )}
      {tab === "limits" && <LimitsTab currency={currency} />}
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
  highlight,
}: {
  label: string;
  value: string;
  hint?: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-background/30 p-3",
        highlight && "border-primary/40 bg-primary/5",
      )}
    >
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={cn("mt-1 font-serif text-2xl leading-none", highlight && "text-primary")}>
        {value}
      </div>
      {hint && <div className="mt-1 text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function Bar({
  value,
  max,
  tone = "primary",
}: {
  value: number;
  max: number;
  tone?: "primary" | "warm";
}) {
  const pct = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-background/60">
      <div
        className="h-full rounded-full"
        style={{
          width: `${pct}%`,
          background:
            tone === "warm"
              ? "linear-gradient(90deg, #FF8A3D, #FF8A3D)"
              : "linear-gradient(90deg, var(--primary), color-mix(in oklab, var(--primary) 60%, transparent))",
          boxShadow: "0 0 10px color-mix(in oklab, var(--primary) 50%, transparent)",
        }}
      />
    </div>
  );
}

function OverviewTab({
  agentRows,
  deptRows,
  currency,
}: {
  agentRows: AgentSpendRow[];
  deptRows: DeptSpendRow[];
  currency: Currency;
}) {
  const t = useT();
  const topAgents = [...agentRows].sort((a, b) => b.costUSD - a.costUSD).slice(0, 5);
  const topAgentMax = topAgents[0]?.costUSD ?? 1;
  const topDept = [...deptRows].sort((a, b) => b.costUSD - a.costUSD);
  const topDeptMax = topDept[0]?.costUSD ?? 1;

  // By-provider aggregation (real agent metadata joined to real usage rows).
  const unknownProvider = t("Unknown", "Unbekannt");
  const byProvider = new Map<string, number>();
  for (const r of agentRows) {
    const key = r.provider ?? unknownProvider;
    byProvider.set(key, (byProvider.get(key) ?? 0) + r.costUSD);
  }
  const providers = [...byProvider.entries()].sort((a, b) => b[1] - a[1]);
  const provMax = providers[0]?.[1] ?? 1;

  const noUsage = t("No usage recorded yet.", "Noch kein Verbrauch erfasst.");

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel className="p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("Top agents", "Top-Agenten")}
            </div>
            <div className="font-serif text-base">{t("By spend", "Nach Ausgaben")}</div>
          </div>
          <Users className="h-4 w-4 text-muted-foreground" />
        </div>
        <div className="space-y-3">
          {topAgents.map((r) => (
            <div key={r.id} className="space-y-1.5">
              <div className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2 min-w-0">
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ background: r.avatarColor }}
                  />
                  <span className="truncate font-medium">{r.name}</span>
                  {r.llm && <span className="text-[11px] text-muted-foreground">· {r.llm}</span>}
                </div>
                <span className="font-mono text-xs">
                  {formatMoney(convert(r.costUSD, currency), currency)}
                </span>
              </div>
              <Bar value={r.costUSD} max={topAgentMax} />
            </div>
          ))}
          {topAgents.length === 0 && (
            <div className="rounded-md border border-dashed border-border/60 px-2 py-3 text-center text-[11px] text-muted-foreground">
              {noUsage}
            </div>
          )}
        </div>
      </Panel>

      <Panel className="p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("Departments", "Abteilungen")}
            </div>
            <div className="font-serif text-base">
              {t("Spend distribution", "Ausgabenverteilung")}
            </div>
          </div>
          <Building2 className="h-4 w-4 text-muted-foreground" />
        </div>
        <div className="space-y-3">
          {topDept.map((d) => {
            const agentCount = agentRows.filter((r) => r.departmentId === d.id).length;
            return (
              <div key={d.id} className="space-y-1.5">
                <div className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2 min-w-0">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ background: d.accent }}
                    />
                    <span className="truncate font-medium">{d.name}</span>
                    <span className="text-[11px] text-muted-foreground">
                      · {agentCount} {t("agents", "Agenten")}
                    </span>
                  </div>
                  <span className="font-mono text-xs">
                    {formatMoney(convert(d.costUSD, currency), currency)}
                  </span>
                </div>
                <Bar value={d.costUSD} max={topDeptMax} />
              </div>
            );
          })}
          {topDept.length === 0 && (
            <div className="rounded-md border border-dashed border-border/60 px-2 py-3 text-center text-[11px] text-muted-foreground">
              {noUsage}
            </div>
          )}
        </div>
      </Panel>

      <Panel className="p-5 lg:col-span-2">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("By provider", "Nach Anbieter")}
            </div>
            <div className="font-serif text-base">
              {t("Where the money goes", "Wohin das Geld fließt")}
            </div>
          </div>
          <Sparkles className="h-4 w-4 text-muted-foreground" />
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {providers.map(([prov, total]) => (
            <div key={prov} className="rounded-lg border border-border bg-background/30 p-4">
              <div className="flex items-center justify-between">
                <div className="text-sm font-medium">{prov}</div>
                <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  {Math.round((total / (provMax || 1)) * 100)}%
                </span>
              </div>
              <div className="mt-2 font-serif text-xl text-primary">
                {formatMoney(convert(total, currency), currency)}
              </div>
              <div className="mt-2">
                <Bar value={total} max={provMax} />
              </div>
            </div>
          ))}
          {providers.length === 0 && (
            <div className="rounded-md border border-dashed border-border/60 px-2 py-3 text-center text-[11px] text-muted-foreground sm:col-span-2 lg:col-span-4">
              {noUsage}
            </div>
          )}
        </div>
      </Panel>

      <ReconciliationCard currency={currency} />
    </div>
  );
}

const ADMIN_KEY_SECRET_RE = /^model\/(anthropic|openai)\/admin_key$/;

// Opt-in validation card (Cost Center §Part C): compares oc8's own recorded
// spend against what the provider itself billed, per provider, over the last
// 30 days. Deliberately placed last and visually secondary -- it must never
// compete with the platform's own recorded numbers above. Renders nothing
// until at least one provider has a connected admin key (see routes/models.tsx).
// Visibility is gated on the admin key existing, NOT on rows existing --
// rows in model_cost_reconciliation are created exclusively by clicking
// Refresh below, so gating on rows.length would make the button itself
// unreachable (chicken-and-egg). When an admin key is connected but no
// refresh has run yet, the card still renders with an empty state.
export function ReconciliationCard({ currency }: { currency: Currency }) {
  const t = useT();
  const { data: secrets = [] } = useSecrets();
  const { data: rows = [] } = useReconciliation();
  const refresh = useRefreshReconciliation();
  const hasAdminKey = secrets.some((s) => ADMIN_KEY_SECRET_RE.test(s.name));

  const byProvider = useMemo(() => {
    const map = new Map<
      string,
      { calculatedMicros: number; reportedMicros: number; hasReported: boolean }
    >();
    for (const r of rows) {
      const entry = map.get(r.provider) ?? {
        calculatedMicros: 0,
        reportedMicros: 0,
        hasReported: false,
      };
      entry.calculatedMicros += r.oc8CalculatedCostMicros;
      if (r.providerReportedCostMicros != null) {
        entry.reportedMicros += r.providerReportedCostMicros;
        entry.hasReported = true;
      }
      map.set(r.provider, entry);
    }
    return [...map.entries()]
      .map(([provider, v]) => ({
        provider,
        calculatedUSD: v.calculatedMicros / 1_000_000,
        reportedUSD: v.reportedMicros / 1_000_000,
        hasReported: v.hasReported,
      }))
      .sort((a, b) => a.provider.localeCompare(b.provider));
  }, [rows]);

  if (!hasAdminKey) return null;

  const handleRefresh = async () => {
    try {
      await refresh.mutateAsync();
      toast.success(t("Reconciliation refreshed", "Abgleich aktualisiert"));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not refresh", "Aktualisierung fehlgeschlagen"),
      );
    }
  };

  return (
    <Panel className="p-5 opacity-90 lg:col-span-2">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            {t("Provider-reported cost", "Vom Anbieter gemeldete Kosten")}
          </div>
          <div className="font-serif text-sm">
            {t("Last 30 days — validation only", "Letzte 30 Tage — nur zur Validierung")}
          </div>
        </div>
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refresh.isPending}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
        >
          <RefreshCw className={cn("h-3 w-3", refresh.isPending && "animate-spin")} />
          {t("Refresh", "Aktualisieren")}
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-md border border-dashed border-border/60 px-2 py-3 text-center text-[11px] text-muted-foreground">
          {t(
            "Not refreshed yet — click Refresh to fetch provider-reported cost.",
            "Noch nicht aktualisiert — auf „Aktualisieren“ klicken, um die vom Anbieter gemeldeten Kosten abzurufen.",
          )}
        </div>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {byProvider.map((p) => {
            const delta = p.reportedUSD - p.calculatedUSD;
            const deltaTone =
              !p.hasReported || Math.abs(delta) < 0.01
                ? "var(--muted-foreground)"
                : "var(--status-warning)";
            return (
              <div
                key={p.provider}
                className="rounded-md border border-border/60 bg-background/20 p-2.5 text-xs"
              >
                <div className="flex items-center justify-between font-medium">
                  <span>{p.provider}</span>
                  {!p.hasReported && (
                    <span className="text-[10px] font-normal text-muted-foreground">
                      {t("no report yet", "noch kein Bericht")}
                    </span>
                  )}
                </div>
                {p.hasReported && (
                  <div className="mt-1 space-y-0.5 text-muted-foreground">
                    <div className="flex items-center justify-between">
                      <span>{t("oc8 calculated", "oc8 berechnet")}</span>
                      <span className="font-mono">
                        {formatMoney(convert(p.calculatedUSD, currency), currency)}
                      </span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span>{t("Provider reported", "Anbieter gemeldet")}</span>
                      <span className="font-mono">
                        {formatMoney(convert(p.reportedUSD, currency), currency)}
                      </span>
                    </div>
                    <div
                      className="flex items-center justify-between font-medium"
                      style={{ color: deltaTone }}
                    >
                      <span>{t("Delta", "Differenz")}</span>
                      <span className="font-mono">
                        {delta >= 0 ? "+" : ""}
                        {formatMoney(convert(delta, currency), currency)}
                      </span>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function AgentsTab({ agentRows, currency }: { agentRows: AgentSpendRow[]; currency: Currency }) {
  const t = useT();
  const sorted = [...agentRows].sort((a, b) => b.costUSD - a.costUSD);
  const max = sorted[0]?.costUSD ?? 1;
  return (
    <Panel className="overflow-hidden">
      <div className="border-b border-border px-5 py-3">
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {t("Cost per agent", "Kosten pro Agent")}
        </div>
        <div className="font-serif text-base">{t("Recorded usage", "Erfasster Verbrauch")}</div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
              <th className="px-5 py-3 font-medium">{t("Agent", "Agent")}</th>
              <th className="px-5 py-3 font-medium">{t("Model", "Modell")}</th>
              <th className="px-5 py-3 font-medium text-right">{t("Input", "Input")}</th>
              <th className="px-5 py-3 font-medium text-right">{t("Output", "Output")}</th>
              <th className="px-5 py-3 font-medium text-right">{t("Cost", "Kosten")}</th>
              <th className="px-5 py-3 font-medium">{t("Share", "Anteil")}</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={r.id} className="border-b border-border/60 last:border-none">
                <td className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full" style={{ background: r.avatarColor }} />
                    <div>
                      <div className="font-medium">{r.name}</div>
                      <div className="text-[11px] text-muted-foreground">
                        {r.role ?? t("unassigned agent", "nicht zugeordneter Agent")}
                        {r.departmentName && ` · ${r.departmentName}`}
                      </div>
                    </div>
                  </div>
                </td>
                <td className="px-5 py-3 font-mono text-xs">{r.llm ?? "—"}</td>
                <td className="px-5 py-3 text-right font-mono text-xs">
                  {formatTokens(r.tokensIn)}
                </td>
                <td className="px-5 py-3 text-right font-mono text-xs">
                  {formatTokens(r.tokensOut)}
                </td>
                <td className="px-5 py-3 text-right font-mono text-sm font-medium text-primary">
                  {formatMoney(convert(r.costUSD, currency), currency)}
                </td>
                <td className="px-5 py-3">
                  <Bar value={r.costUSD} max={max} />
                </td>
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={6} className="px-5 py-10 text-center text-sm text-muted-foreground">
                  {t("No usage recorded yet.", "Noch kein Verbrauch erfasst.")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function DeptTab({
  deptRows,
  agentRows,
  currency,
}: {
  deptRows: DeptSpendRow[];
  agentRows: AgentSpendRow[];
  currency: Currency;
}) {
  const t = useT();
  const sorted = [...deptRows].sort((a, b) => b.costUSD - a.costUSD);
  const max = sorted[0]?.costUSD ?? 1;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {sorted.map((d) => {
        const deptAgents = agentRows
          .filter((r) => r.departmentId === d.id)
          .sort((a, b) => b.costUSD - a.costUSD);
        return (
          <Panel key={d.id} className="p-5">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-3">
                <span
                  className="h-8 w-8 rounded-md"
                  style={{ background: `color-mix(in oklab, ${d.accent} 45%, transparent)` }}
                />
                <div>
                  <div className="font-serif text-lg leading-tight">{d.name}</div>
                  <div className="text-[11px] text-muted-foreground">
                    {deptAgents.length} {t("agents", "Agenten")}
                  </div>
                </div>
              </div>
              <div className="text-right">
                <div className="font-serif text-2xl text-primary">
                  {formatMoney(convert(d.costUSD, currency), currency)}
                </div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Recorded spend", "Erfasste Ausgaben")}
                </div>
                {d.savedCostUSD > 0 && (
                  <div className="mt-2 flex items-center justify-end gap-1.5 text-[11px] text-primary">
                    <span>{t("Saved by caching:", "Durch Caching gespart:")}</span>
                    <span className="font-mono">
                      {formatMoney(convert(d.savedCostUSD, currency), currency)}
                    </span>
                  </div>
                )}
              </div>
            </div>

            <div className="mt-3">
              <Bar value={d.costUSD} max={max} />
            </div>

            <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
              <div className="rounded-md border border-border bg-background/30 p-2">
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Input", "Input")}
                </div>
                <div className="font-mono">{formatTokens(d.tokensIn)}</div>
              </div>
              <div className="rounded-md border border-border bg-background/30 p-2">
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Output", "Output")}
                </div>
                <div className="font-mono">{formatTokens(d.tokensOut)}</div>
              </div>
            </div>

            <div className="mt-4">
              <div className="mb-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
                {t("Agents", "Agenten")}
              </div>
              <div className="space-y-1.5">
                {deptAgents.map((r) => (
                  <div
                    key={r.id}
                    className="flex items-center justify-between rounded-md border border-border/60 bg-background/20 px-2 py-1.5 text-xs"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className="h-1.5 w-1.5 rounded-full"
                        style={{ background: r.avatarColor }}
                      />
                      <span className="truncate">{r.name}</span>
                      {r.llm && (
                        <span className="truncate text-[10px] text-muted-foreground">{r.llm}</span>
                      )}
                    </div>
                    <span className="font-mono">
                      {formatMoney(convert(r.costUSD, currency), currency)}
                    </span>
                  </div>
                ))}
                {deptAgents.length === 0 && (
                  <div className="rounded-md border border-dashed border-border/60 px-2 py-3 text-center text-[11px] text-muted-foreground">
                    {t("No usage recorded yet.", "Noch kein Verbrauch erfasst.")}
                  </div>
                )}
              </div>
            </div>
          </Panel>
        );
      })}
      {sorted.length === 0 && (
        <div className="rounded-lg border border-dashed border-border/60 px-5 py-10 text-center text-sm text-muted-foreground md:col-span-2">
          {t("No usage recorded yet.", "Noch kein Verbrauch erfasst.")}
        </div>
      )}
    </div>
  );
}

function LimitsTab({ currency }: { currency: Currency }) {
  const t = useT();
  const [pricing] = usePricing();
  const [limits, setLimits] = useLimits();
  const [addOpen, setAddOpen] = useState(false);
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });

  const update = (id: string, patch: Partial<CostLimit>) => {
    setLimits(limits.map((l) => (l.id === id ? { ...l, ...patch } : l)));
  };
  const remove = (id: string) => {
    setLimits(limits.filter((l) => l.id !== id));
    toast(t("Limit removed", "Limit entfernt"));
  };

  const rows = limits.map((l) => {
    const spend = currentSpendForLimit(l, pricing);
    const pct = l.amountUSD > 0 ? Math.min(100, Math.round((spend / l.amountUSD) * 100)) : 0;
    return { limit: l, spend, pct };
  });

  const scopes: { id: LimitScope; label: string; icon: typeof Users }[] = [
    { id: "agent", label: t("Agent", "Agent"), icon: Users },
    { id: "department", label: t("Department", "Abteilung"), icon: Building2 },
    { id: "model", label: t("Model (LLM)", "Modell (LLM)"), icon: Cpu },
  ];

  const targetLabel = (l: CostLimit) => {
    if (l.scope === "agent")
      return agentsPage?.items?.find((a) => a.id === l.targetId)?.name ?? l.targetId;
    if (l.scope === "department")
      return departmentsPage?.items?.find((d) => d.id === l.targetId)?.name ?? l.targetId;
    return l.targetId;
  };

  const summary = scopes.map((s) => ({
    ...s,
    count: limits.filter((l) => l.scope === s.id).length,
  }));

  return (
    <div className="space-y-4">
      <LiveBudgetsPanel currency={currency} />
      <Panel className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
              <ShieldAlert className="h-4 w-4" />
            </div>
            <div>
              <div className="font-serif text-lg leading-tight">
                {t("Cost-limit preview", "Kostenlimit-Vorschau")}
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {t(
                  "This legacy preview is read-only. The live token budgets above are the only enforced budget controls until a cost-limit API exists.",
                  "Diese Altansicht ist schreibgeschützt. Die Live-Token-Budgets oben sind bis zu einer Kostenlimit-API die einzigen durchgesetzten Budgetkontrollen.",
                )}
              </p>
              <p className="mt-1 text-[11px] italic text-muted-foreground">
                {t(
                  "Usage below is an estimate from the editable pricing model (see Models), not the platform's recorded spend — see the Overview/By Agent/By Department tabs for metered figures.",
                  "Der Verbrauch unten ist eine Schätzung anhand des editierbaren Preismodells (siehe Modelle), nicht die vom System erfassten Ausgaben — gemessene Werte finden Sie unter Übersicht/Pro Agent/Pro Abteilung.",
                )}
              </p>
              <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
                {summary.map((s) => {
                  const Icon = s.icon;
                  return (
                    <span
                      key={s.id}
                      className="inline-flex items-center gap-1 rounded-full border border-border bg-background/40 px-2 py-0.5"
                    >
                      <Icon className="h-3 w-3" />
                      {s.label} · {s.count}
                    </span>
                  );
                })}
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setAddOpen(true)}
            disabled
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:brightness-110"
          >
            <Plus className="h-4 w-4" /> {t("Add limit", "Limit hinzufügen")}
          </button>
        </div>
      </Panel>

      <Panel className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-5 py-3 font-medium">{t("Scope", "Ebene")}</th>
                <th className="px-5 py-3 font-medium">{t("Target", "Ziel")}</th>
                <th className="px-5 py-3 font-medium text-right">{t("Limit", "Limit")}</th>
                <th className="px-5 py-3 font-medium">{t("Period", "Zeitraum")}</th>
                <th className="px-5 py-3 font-medium">{t("Action", "Aktion")}</th>
                <th className="px-5 py-3 font-medium">
                  {t("Usage (est.)", "Verbrauch (geschätzt)")}
                </th>
                <th className="px-5 py-3 font-medium text-right">{t("Enabled", "Aktiv")}</th>
                <th className="px-5 py-3 font-medium" />
              </tr>
            </thead>
            <tbody>
              {rows.map(({ limit, spend, pct }) => {
                const scopeMeta = scopes.find((s) => s.id === limit.scope)!;
                const Icon = scopeMeta.icon;
                const tone =
                  pct >= 100
                    ? "var(--status-error)"
                    : pct >= 80
                      ? "var(--status-warning)"
                      : "var(--status-running)";
                return (
                  <tr key={limit.id} className="border-b border-border/60 last:border-none">
                    <td className="px-5 py-3">
                      <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-background/40 px-2 py-0.5 text-xs">
                        <Icon className="h-3 w-3" />
                        {scopeMeta.label}
                      </span>
                    </td>
                    <td className="px-5 py-3">
                      <TargetSelect
                        scope={limit.scope}
                        value={limit.targetId}
                        disabled
                        onChange={(v) => update(limit.id, { targetId: v })}
                      />
                    </td>
                    <td className="px-5 py-3">
                      <div className="flex items-center justify-end gap-1">
                        <span className="text-muted-foreground">
                          {currency === "USD" ? "$" : "€"}
                        </span>
                        <input
                          disabled
                          type="number"
                          step="10"
                          min="0"
                          value={
                            currency === "USD"
                              ? limit.amountUSD
                              : Number((limit.amountUSD * 0.92).toFixed(2))
                          }
                          onChange={(e) => {
                            const val = parseFloat(e.target.value) || 0;
                            const usd = currency === "USD" ? val : val / 0.92;
                            update(limit.id, { amountUSD: usd });
                          }}
                          className="w-24 rounded-md border border-border bg-background/40 px-2 py-1 text-right font-mono text-xs focus:border-primary focus:outline-none"
                        />
                      </div>
                    </td>
                    <td className="px-5 py-3">
                      <select
                        disabled
                        value={limit.period}
                        onChange={(e) =>
                          update(limit.id, { period: e.target.value as LimitPeriod })
                        }
                        className="rounded-md border border-border bg-background/40 px-2 py-1 text-xs focus:border-primary focus:outline-none"
                      >
                        <option value="daily">{t("Daily", "Täglich")}</option>
                        <option value="monthly">{t("Monthly", "Monatlich")}</option>
                      </select>
                    </td>
                    <td className="px-5 py-3">
                      <select
                        disabled
                        value={limit.action}
                        onChange={(e) =>
                          update(limit.id, { action: e.target.value as LimitAction })
                        }
                        className="rounded-md border border-border bg-background/40 px-2 py-1 text-xs focus:border-primary focus:outline-none"
                      >
                        <option value="notify">{t("Notify", "Benachrichtigen")}</option>
                        <option value="throttle">{t("Throttle", "Drosseln")}</option>
                        <option value="block">{t("Block", "Sperren")}</option>
                      </select>
                    </td>
                    <td className="px-5 py-3">
                      <div className="min-w-[140px]">
                        <div className="mb-1 flex items-center justify-between text-[11px]">
                          <span className="font-mono" style={{ color: tone }}>
                            {formatMoney(convert(spend, currency), currency)}
                          </span>
                          <span className="text-muted-foreground">{pct}%</span>
                        </div>
                        <div className="h-1.5 w-full overflow-hidden rounded-full bg-background/60">
                          <div
                            className="h-full rounded-full"
                            style={{
                              width: `${Math.max(2, pct)}%`,
                              background: tone,
                              boxShadow: `0 0 8px ${tone}`,
                            }}
                          />
                        </div>
                      </div>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <button
                        type="button"
                        role="switch"
                        aria-checked={limit.enabled}
                        onClick={() => update(limit.id, { enabled: !limit.enabled })}
                        disabled
                        className={cn(
                          "relative inline-flex h-5 w-9 items-center rounded-full transition-colors",
                          limit.enabled ? "bg-primary" : "bg-border",
                        )}
                      >
                        <span
                          className={cn(
                            "inline-block h-4 w-4 transform rounded-full bg-background transition-transform",
                            limit.enabled ? "translate-x-4" : "translate-x-0.5",
                          )}
                        />
                      </button>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <button
                        type="button"
                        onClick={() => remove(limit.id)}
                        disabled
                        className="rounded-md p-1.5 text-muted-foreground hover:bg-background/40 hover:text-[color:var(--status-error)]"
                        aria-label={t("Delete limit", "Limit löschen")}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                );
              })}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-5 py-10 text-center text-sm text-muted-foreground">
                    {t(
                      "No limits set yet. Add one to protect your budget.",
                      "Noch keine Limits gesetzt. Fügen Sie eines hinzu, um Ihr Budget zu schützen.",
                    )}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center gap-3 border-t border-border px-5 py-3 text-[11px] text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <Bell className="h-3 w-3" />
            {t("Notify — alert only", "Benachrichtigen — nur Hinweis")}
          </span>
          <span className="inline-flex items-center gap-1">
            <Gauge className="h-3 w-3" />
            {t("Throttle — slow requests", "Drosseln — Anfragen bremsen")}
          </span>
          <span className="inline-flex items-center gap-1">
            <Ban className="h-3 w-3" />
            {t("Block — hard stop", "Sperren — harter Stopp")}
          </span>
        </div>
      </Panel>

      {addOpen && (
        <AddLimitDialog
          onClose={() => setAddOpen(false)}
          onCreate={(l) => {
            setLimits([...limits, l]);
            setAddOpen(false);
            toast.success(t("Limit created", "Limit erstellt"));
          }}
        />
      )}
    </div>
  );
}

function LiveBudgetsPanel({ currency }: { currency: Currency }) {
  const t = useT();
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items;
  const { data: budgets } = useBudgets();
  const budgetFor = (departmentId: string | null) =>
    budgets?.find((b) => b.departmentId === departmentId);

  return (
    <Panel className="p-5">
      <div className="mb-4 flex items-start gap-3">
        <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
          <Gauge className="h-4 w-4" />
        </div>
        <div>
          <div className="font-serif text-lg leading-tight">
            {t("Token Budgets (live)", "Token-Budgets (live)")}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {t(
              "Enforced by the platform — a hard limit pauses new tasks with budget_exceeded.",
              "Vom System durchgesetzt — ein hartes Limit pausiert neue Tasks mit budget_exceeded.",
            )}
          </p>
        </div>
      </div>
      <div className="space-y-3">
        <BudgetRow
          label={t("Tenant-wide", "Gesamter Tenant")}
          departmentId={null}
          budget={budgetFor(null)}
          currency={currency}
        />
        {(departments ?? []).map((d) => (
          <BudgetRow
            key={d.id}
            label={d.name}
            departmentId={d.id}
            budget={budgetFor(d.id)}
            currency={currency}
          />
        ))}
      </div>
    </Panel>
  );
}

function BudgetRow({
  label,
  departmentId,
  budget,
  currency,
}: {
  label: string;
  departmentId: string | null;
  budget?: BudgetDTO;
  currency: Currency;
}) {
  const t = useT();
  const { data: status } = useBudgetStatus(departmentId);
  const setBudget = useSetBudget();
  const [mode, setMode] = useState<"tokens" | "dollar">("tokens");
  const [soft, setSoft] = useState<string>("");
  const [hard, setHard] = useState<string>("");
  const [dollar, setDollar] = useState<string>("");
  const currencySymbol = currency === "USD" ? "$" : "€";

  useEffect(() => {
    if (status) {
      setSoft(status.softLimitTokens?.toString() ?? "");
      setHard(status.hardLimitTokens?.toString() ?? "");
    }
  }, [status]);

  // The stored value is always USD (`dollarBudgetUsd`) -- the input only
  // ever displays it converted to the page-wide `currency` toggle, mirroring
  // every other money value on this page (see `convert`/`formatMoney` call
  // sites in OverviewTab/AgentsTab/DeptTab above).
  useEffect(() => {
    if (budget?.dollarBudgetUsd != null) {
      setMode("dollar");
      setDollar(convert(budget.dollarBudgetUsd, currency).toFixed(2));
    }
  }, [budget, currency]);

  const pct =
    status?.hardLimitTokens && status.hardLimitTokens > 0
      ? Math.min(100, Math.round((status.currentTokens / status.hardLimitTokens) * 100))
      : 0;
  const tone = status?.hardExceeded
    ? "var(--status-error)"
    : status?.softExceeded
      ? "var(--status-warning)"
      : "var(--status-running)";

  const save = async () => {
    if (mode === "dollar") {
      const parsed = parseFloat(dollar);
      if (Number.isNaN(parsed)) return;
      // Convert the displayed (possibly EUR) amount back to the USD the
      // backend stores and enforces against.
      const usd = currency === "USD" ? parsed : parsed / FX_USD_EUR;
      try {
        await setBudget.mutateAsync({ departmentId, dollarBudgetUsd: usd });
      } catch (err) {
        toast.error(
          err instanceof Error
            ? err.message
            : t("Could not save budget", "Budget konnte nicht gespeichert werden"),
        );
      }
    } else {
      setBudget.mutate({
        departmentId,
        softLimitTokens: soft.trim() === "" ? null : parseInt(soft, 10),
        hardLimitTokens: hard.trim() === "" ? null : parseInt(hard, 10),
      });
    }
  };

  return (
    <div className="rounded-lg border border-border bg-background/30 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{label}</div>
          <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
            {formatTokens(status?.currentTokens ?? 0)}{" "}
            {t("used this month", "verbraucht diesen Monat")}
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <div className="inline-flex rounded-md border border-border bg-background/40 p-0.5">
            {(["tokens", "dollar"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={cn(
                  "rounded px-2 py-1 font-medium transition",
                  mode === m
                    ? "bg-primary/20 text-primary"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {m === "tokens" ? t("Tokens", "Token") : currencySymbol}
              </button>
            ))}
          </div>
          {mode === "dollar" ? (
            <input
              type="number"
              min="0"
              step="0.01"
              placeholder={t(`${currency} / month`, `${currency} / Monat`)}
              value={dollar}
              onChange={(e) => setDollar(e.target.value)}
              className="w-28 rounded-md border border-border bg-background/40 px-2 py-1 text-right font-mono focus:border-primary focus:outline-none"
            />
          ) : (
            <>
              <input
                type="number"
                min="0"
                placeholder={t("soft", "weich")}
                value={soft}
                onChange={(e) => setSoft(e.target.value)}
                className="w-24 rounded-md border border-border bg-background/40 px-2 py-1 text-right font-mono focus:border-primary focus:outline-none"
              />
              <input
                type="number"
                min="0"
                placeholder={t("hard", "hart")}
                value={hard}
                onChange={(e) => setHard(e.target.value)}
                className="w-24 rounded-md border border-border bg-background/40 px-2 py-1 text-right font-mono focus:border-primary focus:outline-none"
              />
            </>
          )}
          <button
            type="button"
            onClick={save}
            disabled={setBudget.isPending || (mode === "dollar" && dollar.trim() === "")}
            className="rounded-md bg-primary px-2.5 py-1 text-primary-foreground hover:brightness-110 disabled:opacity-50"
          >
            {t("Save", "Speichern")}
          </button>
        </div>
      </div>
      {mode === "dollar" && budget?.dollarBudgetUsd != null && budget.hardLimitTokens != null && (
        <div className="mt-1.5 text-[11px] text-muted-foreground">
          {t(
            `≈ ${formatTokens(budget.hardLimitTokens)} tokens (at ${budget.dollarReferenceModel} pricing)`,
            `≈ ${formatTokens(budget.hardLimitTokens)} Token (zu ${budget.dollarReferenceModel}-Preisen)`,
          )}
        </div>
      )}
      {status?.hardLimitTokens != null && (
        <div className="mt-2">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-background/60">
            <div
              className="h-full rounded-full"
              style={{
                width: `${Math.max(2, pct)}%`,
                background: tone,
                boxShadow: `0 0 8px ${tone}`,
              }}
            />
          </div>
        </div>
      )}
      {status?.hardExceeded && (
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-[color:var(--status-error)]">
          <ShieldAlert className="h-3 w-3" />
          {t(
            "Hard limit exceeded — new tasks are paused.",
            "Hartes Limit überschritten — neue Tasks pausiert.",
          )}
        </div>
      )}
    </div>
  );
}

function TargetSelect({
  scope,
  value,
  disabled = false,
  onChange,
}: {
  scope: LimitScope;
  value: string;
  disabled?: boolean;
  onChange: (v: string) => void;
}) {
  const [pricing] = usePricing();
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const options =
    scope === "agent"
      ? (agentsPage?.items ?? []).map((a) => ({ id: a.id, label: `${a.name} · ${a.role}` }))
      : scope === "department"
        ? (departmentsPage?.items ?? []).map((d) => ({ id: d.id, label: d.name }))
        : pricing.map((p) => ({ id: p.id, label: p.id }));
  return (
    <select
      disabled={disabled}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="max-w-[220px] rounded-md border border-border bg-background/40 px-2 py-1 text-xs focus:border-primary focus:outline-none"
    >
      {options.map((o) => (
        <option key={o.id} value={o.id}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function AddLimitDialog({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (l: CostLimit) => void;
}) {
  const t = useT();
  const [pricing] = usePricing();
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const [scope, setScope] = useState<LimitScope>("agent");
  const defaultTarget = (s: LimitScope) =>
    s === "agent"
      ? (agentsPage?.items?.[0]?.id ?? "")
      : s === "department"
        ? (departmentsPage?.items?.[0]?.id ?? "")
        : (pricing[0]?.id ?? "");
  const [targetId, setTargetId] = useState<string>(defaultTarget("agent"));
  const [amount, setAmount] = useState(100);
  const [period, setPeriod] = useState<LimitPeriod>("monthly");
  const [action, setAction] = useState<LimitAction>("notify");

  // Agents/departments load async; backfill the default target once they
  // arrive if the user hasn't picked one yet.
  useEffect(() => {
    if (!targetId) setTargetId(defaultTarget(scope));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentsPage, departmentsPage]);

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-border bg-panel p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-primary" />
          <div className="font-serif text-lg">{t("New cost limit", "Neues Kostenlimit")}</div>
        </div>

        <div className="space-y-3 text-sm">
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-widest text-muted-foreground">
              {t("Scope", "Ebene")}
            </label>
            <div className="grid grid-cols-3 gap-1 rounded-md border border-border bg-background/40 p-0.5">
              {(["agent", "department", "model"] as LimitScope[]).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => {
                    setScope(s);
                    setTargetId(defaultTarget(s));
                  }}
                  className={cn(
                    "rounded px-2 py-1 text-xs transition",
                    scope === s
                      ? "bg-primary/20 text-primary"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {s === "agent"
                    ? t("Agent", "Agent")
                    : s === "department"
                      ? t("Department", "Abteilung")
                      : t("Model", "Modell")}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-widest text-muted-foreground">
              {t("Target", "Ziel")}
            </label>
            <TargetSelect scope={scope} value={targetId} onChange={setTargetId} />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-[11px] uppercase tracking-widest text-muted-foreground">
                {t("Amount (USD)", "Betrag (USD)")}
              </label>
              <input
                type="number"
                min="0"
                step="10"
                value={amount}
                onChange={(e) => setAmount(parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm focus:border-primary focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-[11px] uppercase tracking-widest text-muted-foreground">
                {t("Period", "Zeitraum")}
              </label>
              <select
                value={period}
                onChange={(e) => setPeriod(e.target.value as LimitPeriod)}
                className="w-full rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm focus:border-primary focus:outline-none"
              >
                <option value="daily">{t("Daily", "Täglich")}</option>
                <option value="monthly">{t("Monthly", "Monatlich")}</option>
              </select>
            </div>
          </div>

          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-widest text-muted-foreground">
              {t("Action on threshold", "Aktion bei Schwelle")}
            </label>
            <select
              value={action}
              onChange={(e) => setAction(e.target.value as LimitAction)}
              className="w-full rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm focus:border-primary focus:outline-none"
            >
              <option value="notify">{t("Notify", "Benachrichtigen")}</option>
              <option value="throttle">{t("Throttle", "Drosseln")}</option>
              <option value="block">{t("Block", "Sperren")}</option>
            </select>
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border bg-background/40 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            type="button"
            onClick={() =>
              onCreate({
                id: `l-${Date.now()}`,
                scope,
                targetId,
                amountUSD: amount,
                period,
                action,
                enabled: true,
              })
            }
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:brightness-110"
          >
            {t("Create limit", "Limit erstellen")}
          </button>
        </div>
      </div>
    </div>
  );
}
