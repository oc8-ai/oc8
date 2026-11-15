import { WidgetTitle } from "@/components/dashboard/widget-title";
import { useBoardQuery } from "@/lib/boards-api";
import type { BoardQuery, DatasetSpec, KpiBlock as KpiSpec } from "@/lib/boards-api";
import { useLang, useT } from "@/lib/i18n";

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
  const { data, error } = useBoardQuery(boardId, query);
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
        <div className="flex flex-col gap-1 px-3 pb-3">
          <div className="font-mono text-2xl tabular-nums">
            {value === null ? NO_VALUE : formatValue(value, field, lang)}
          </div>
          {value === null ? (
            <div className="text-xs text-muted-foreground">{t("No data", "Keine Daten")}</div>
          ) : change !== null ? (
            <div className="flex items-baseline gap-1.5 text-xs">
              <span className="font-mono tabular-nums">{formatChange(change, lang)}</span>
              <span className="text-muted-foreground">{against}</span>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
