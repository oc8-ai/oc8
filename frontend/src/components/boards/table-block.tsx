import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";

import { WidgetTitle } from "@/components/dashboard/widget-title";
import { useBoardQuery } from "@/lib/boards-api";
import type { BoardQuery, DatasetSpec, TableBlock as TableSpec } from "@/lib/boards-api";
import { useLang, useT } from "@/lib/i18n";

import { BlockMessage, BlockStatus } from "./block-status";
import { formatAsOf, formatValue } from "./board-format";

type Sort = { field: string; dir: "asc" | "desc" };

/** A dataset's rows, paged and sorted on the server. */
export function TableBlock({
  boardId,
  block,
  dataset,
  query,
}: {
  boardId: string;
  block: TableSpec;
  dataset: DatasetSpec;
  query: BoardQuery | null;
}) {
  const t = useT();
  const { lang } = useLang();
  const limit = query?.page?.limit ?? block.page_size ?? 25;
  // Paging and sorting belong to the filtered query they were chosen for: a
  // filter change starts again at the first page.
  const baseKey = JSON.stringify(query);
  const [view, setView] = useState<{ key: string; offset: number; sort: Sort | null }>({
    key: baseKey,
    offset: 0,
    sort: null,
  });
  const current = view.key === baseKey ? view : { key: baseKey, offset: 0, sort: view.sort };
  const { data, error } = useBoardQuery(
    boardId,
    query ? { ...query, page: { offset: current.offset, limit }, sort: current.sort } : null,
  );

  const toggleSort = (field: string) => {
    const dir = current.sort?.field === field && current.sort.dir === "asc" ? "desc" : "asc";
    setView({ key: baseKey, offset: 0, sort: { field, dir } });
  };
  const goTo = (offset: number) => setView({ ...current, offset });

  const columns = [
    { key: "as_of", label: t("Date", "Datum") },
    ...block.fields.map((key) => ({
      key,
      label: dataset.fields.find((f) => f.key === key)?.label ?? key,
    })),
  ];
  const total = data?.totalRows ?? 0;
  const rows = data?.rows ?? [];

  return (
    <div className="flex h-full min-w-0 flex-col">
      <WidgetTitle title={block.title || dataset.label} />
      <BlockStatus data={data} error={error} t={t} />
      {data &&
        (rows.length === 0 ? (
          <BlockMessage>
            {t("No data for this period", "Keine Daten für diesen Zeitraum")}
          </BlockMessage>
        ) : (
          <div className="flex min-w-0 flex-col gap-1 px-3 pb-2">
            <div className="min-w-0 overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr>
                    {columns.map((c) => {
                      const sorted = current.sort?.field === c.key ? current.sort.dir : null;
                      return (
                        <th
                          key={c.key}
                          scope="col"
                          aria-sort={
                            sorted === "asc"
                              ? "ascending"
                              : sorted === "desc"
                                ? "descending"
                                : "none"
                          }
                          className="py-1 pr-3 text-left font-medium text-muted-foreground"
                        >
                          <button
                            type="button"
                            onClick={() => toggleSort(c.key)}
                            className="inline-flex items-center gap-1 hover:text-foreground"
                          >
                            {c.label}
                            {sorted === "asc" && <ArrowUp aria-hidden className="size-3" />}
                            {sorted === "desc" && <ArrowDown aria-hidden className="size-3" />}
                          </button>
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr key={i} className="border-t border-border/50">
                      {columns.map((c) => {
                        const field = dataset.fields.find((f) => f.key === c.key);
                        const value = row[c.key];
                        const numeric =
                          field?.type === "number" ||
                          field?.type === "money" ||
                          field?.type === "percent";
                        return (
                          <td
                            key={c.key}
                            className={
                              numeric ? "py-1 pr-3 text-right font-mono tabular-nums" : "py-1 pr-3"
                            }
                          >
                            {c.key === "as_of" && typeof value === "string"
                              ? formatAsOf(value, dataset.grain, lang)
                              : formatValue(value, field, lang)}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-end gap-1 text-xs text-muted-foreground">
              <span className="mr-1 tabular-nums">
                {t("{from}–{to} of {total}", "{from}–{to} von {total}")
                  .replace("{from}", String(current.offset + 1))
                  .replace("{to}", String(current.offset + rows.length))
                  .replace("{total}", String(total))}
              </span>
              <button
                type="button"
                aria-label={t("Previous page", "Vorherige Seite")}
                disabled={current.offset === 0}
                onClick={() => goTo(Math.max(0, current.offset - limit))}
                className="rounded p-1 hover:text-foreground disabled:opacity-40"
              >
                <ChevronLeft className="size-3.5" />
              </button>
              <button
                type="button"
                aria-label={t("Next page", "Nächste Seite")}
                disabled={current.offset + limit >= total}
                onClick={() => goTo(current.offset + limit)}
                className="rounded p-1 hover:text-foreground disabled:opacity-40"
              >
                <ChevronRight className="size-3.5" />
              </button>
            </div>
          </div>
        ))}
    </div>
  );
}
