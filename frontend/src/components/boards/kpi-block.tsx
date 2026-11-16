import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";

import { WidgetTitle } from "@/components/dashboard/widget-title";
import { useBoardQuery } from "@/lib/boards-api";
import type { BoardQuery, DatasetSpec, KpiBlock as KpiSpec } from "@/lib/boards-api";
import { useLang, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { BlockStatus } from "./block-status";
import { formatChange, formatValue, NO_VALUE } from "./board-format";

/** One field's total over the period, with its change against the comparison. */
export function KpiBlock({
  boardId,
  block,
  dataset,
  query,
}: {
  boardId: string;
  block: KpiSpec;
  dataset: DatasetSpec;
  query: BoardQuery | null;
}) {
  const t = useT();
  const { lang } = useLang();
  const { data, error, isPlaceholderData } = useBoardQuery(boardId, query);
  const field = dataset.fields.find((f) => f.key === block.field);
  const title = block.title || field?.label || block.field;

  // An ungrouped aggregate always has one total row; its value is null when
  // the period has no records.
  const value = data?.totals.find((r) => r.group === null)?.values[block.field] ?? null;
  const change = data?.compare?.change.find((r) => r.group === null)?.values[block.field] ?? null;
  const against =
    query?.compare === "same_period_last_year"
      ? t("vs. same period last year", "ggü. Vorjahreszeitraum")
      : t("vs. previous period", "ggü. Vorperiode");

  return (
    <div className="flex h-full flex-col">
      <WidgetTitle title={title} />
      <BlockStatus data={data} error={error} t={t} />
      {data && (
        // While the next period loads, the last value stays, dimmed, and its
        // change (which belongs to the old comparison) is hidden.
        <div
          aria-busy={isPlaceholderData}
          className={cn(
            "flex flex-col gap-1.5 px-3 pb-3 transition-opacity",
            isPlaceholderData && "opacity-50",
          )}
        >
          <div className="truncate text-3xl leading-tight font-semibold tracking-tight tabular-nums">
            {value === null ? NO_VALUE : formatValue(value, field, lang)}
          </div>
          {value === null ? (
            <div className="text-xs text-muted-foreground">{t("No data", "Keine Daten")}</div>
          ) : change !== null && !isPlaceholderData ? (
            <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs">
              <ChangeBadge change={change} text={formatChange(change, lang)} />
              <span className="text-muted-foreground">{against}</span>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

/** The change as a signed percentage beside an arrow, so the direction never
 *  rests on colour alone. The text stays in the text colour; only the arrow
 *  and a light wash carry the status colour (mint is too light to read as
 *  text on a light surface). */
function ChangeBadge({ change, text }: { change: number; text: string }) {
  const trend = change > 0 ? "up" : change < 0 ? "down" : "flat";
  const Icon = trend === "up" ? ArrowUpRight : trend === "down" ? ArrowDownRight : Minus;
  return (
    <span
      data-trend={trend}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 font-medium tabular-nums",
        trend === "up" && "bg-[color:var(--status-running)]/15",
        trend === "down" && "bg-[color:var(--status-error)]/12",
        trend === "flat" && "bg-muted",
      )}
    >
      <Icon
        aria-hidden
        className={cn(
          "size-3.5",
          trend === "up" && "text-[color:var(--status-running)]",
          trend === "down" && "text-[color:var(--status-error)]",
          trend === "flat" && "text-muted-foreground",
        )}
      />
      {text}
    </span>
  );
}
