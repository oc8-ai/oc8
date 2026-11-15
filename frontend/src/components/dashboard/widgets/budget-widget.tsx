import { useBudgetStatus } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import type { ComponentProps } from "react";
import { WidgetTitle } from "@/components/dashboard/widget-title";

// Every widget draws its own one-line title; the frame only adds a header in
// edit mode. The body keeps its own loading/empty/error branches.
export function BudgetWidget(props: ComponentProps<typeof BudgetWidgetBody>) {
  const t = useT();
  return (
    <div className="flex h-full flex-col">
      <WidgetTitle title={t("Budget", "Budget")} />
      <div className="min-h-0 flex-1 overflow-hidden">
        <BudgetWidgetBody {...props} />
      </div>
    </div>
  );
}

function BudgetWidgetBody({
  config,
  onConfigChange,
}: {
  config: Record<string, unknown>;
  onConfigChange: (config: Record<string, unknown>) => void;
}) {
  const t = useT();
  const departmentId = typeof config.departmentId === "string" ? config.departmentId : null;
  const { data: status, isPending } = useBudgetStatus(departmentId);

  if (isPending || !status) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-xs text-muted-foreground">
        {t("Loading…", "Wird geladen…")}
      </div>
    );
  }

  const limit = status.hardLimitTokens ?? status.softLimitTokens;
  // `limit === 0` is a real, already-exhausted limit, not "no limit set" --
  // checked explicitly so it doesn't fall through the `? :` as falsy.
  const pct =
    limit == null
      ? 0
      : limit === 0
        ? 100
        : Math.min(100, Math.round((status.currentTokens / limit) * 100));

  return (
    <div className="flex h-full flex-col justify-center gap-2 p-4">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {status.scope === "tenant"
          ? t("Tenant-wide", "Unternehmensweit")
          : t("Department", "Abteilung")}
      </div>
      <div className="font-mono text-sm tabular-nums">
        {status.currentTokens.toLocaleString()}
        {limit != null ? ` / ${limit.toLocaleString()}` : ""}
      </div>
      {limit != null && (
        <div className="h-2 w-full overflow-hidden rounded-full bg-muted/40">
          <div
            className={cn(
              "h-full rounded-full",
              status.hardExceeded
                ? "bg-[color:var(--status-error)]"
                : status.softExceeded
                  ? "bg-[color:var(--status-warning)]"
                  : "bg-primary",
            )}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {status.hardExceeded && (
        <div className="text-xs text-[color:var(--status-error)]">
          {t("Hard limit exceeded", "Hartes Limit überschritten")}
        </div>
      )}
    </div>
  );
}
