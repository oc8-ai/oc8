import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";

import { WidgetTitle } from "@/components/dashboard/widget-title";
import { useBoardQuery } from "@/lib/boards-api";
import type { BoardQuery, DatasetSpec, FieldSpec, TableBlock as TableSpec } from "@/lib/boards-api";
import { useLang, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { BlockMessage, BlockStatus } from "./block-status";
import { formatAsOf, formatValue, NO_VALUE } from "./board-format";

type Sort = { field: string; dir: "asc" | "desc" };

/** Numbers, money and percentages line up on the right, digit under digit. */
function isNumeric(field: FieldSpec | undefined): boolean {
  return field?.type === "number" || field?.type === "money" || field?.type === "percent";
}

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
  // Newest first, as the server sorts by default; the header shows it.
  const [view, setView] = useState<{ key: string; offset: number; sort: Sort }>({
    key: baseKey,
    offset: 0,
    sort: { field: "as_of", dir: "desc" },
  });
  const current = view.key === baseKey ? view : { key: baseKey, offset: 0, sort: view.sort };
  const { data, error, isPlaceholderData } = useBoardQuery(
    boardId,
    query ? { ...query, page: { offset: current.offset, limit }, sort: current.sort } : null,
  );
  // The offset of the rows on screen: while the next page loads, the previous
  // page stays shown, and the range label must describe it, not the next one.
  const [shownOffset, setShownOffset] = useState(current.offset);
  if (data && !isPlaceholderData && shownOffset !== current.offset) {
    setShownOffset(current.offset);
  }
  const rangeOffset = isPlaceholderData ? shownOffset : current.offset;

  const toggleSort = (field: string) => {
    const dir = current.sort.field === field && current.sort.dir === "asc" ? "desc" : "asc";
    setView({ key: baseKey, offset: 0, sort: { field, dir } });
  };
  const goTo = (offset: number) => setView({ ...current, offset });

  // The row's own date is "As of", not "Date": a dataset often has a date
  // field of its own, and two columns named "Date" can't be told apart.
  const columns = [
    { key: "as_of", label: t("As of", "Stand"), field: undefined as FieldSpec | undefined },
    ...block.fields.map((key) => {
      const field = dataset.fields.find((f) => f.key === key);
      return { key, label: field?.label ?? key, field };
    }),
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
            {/* Scrolls inside itself, so the header can stay in view. */}
            <div className="max-h-80 min-w-0 overflow-auto rounded-md">
              <table className="w-full text-xs">
                <thead className="sticky top-0 z-10 bg-muted">
                  <tr>
                    {columns.map((c) => {
                      const sorted = current.sort?.field === c.key ? current.sort.dir : null;
                      const numeric = isNumeric(c.field);
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
                          className={cn(
                            "px-2 py-1.5 font-medium whitespace-nowrap text-muted-foreground",
                            numeric ? "text-right" : "text-left",
                          )}
                        >
                          <button
                            type="button"
                            onClick={() => toggleSort(c.key)}
                            className={cn(
                              "inline-flex items-center gap-1 hover:text-foreground",
                              numeric && "flex-row-reverse",
                            )}
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
                    <tr key={i} className="even:bg-muted/50">
                      {columns.map((c) => {
                        const value = row[c.key];
                        if (c.key === "as_of") {
                          return (
                            <td
                              key={c.key}
                              className="px-2 py-1.5 whitespace-nowrap text-muted-foreground tabular-nums"
                            >
                              {typeof value === "string"
                                ? formatAsOf(value, dataset.grain, lang)
                                : formatValue(value, undefined, lang)}
                            </td>
                          );
                        }
                        const text = formatValue(value, c.field, lang);
                        if (isNumeric(c.field)) {
                          return (
                            <td
                              key={c.key}
                              className="px-2 py-1.5 text-right whitespace-nowrap tabular-nums"
                            >
                              {text}
                            </td>
                          );
                        }
                        return (
                          <td key={c.key} className="px-2 py-1.5">
                            {c.field?.type === "category" && text !== NO_VALUE ? (
                              <span
                                data-chip
                                className="inline-block max-w-40 truncate rounded-full bg-foreground/[0.07] px-2 py-0.5 align-middle"
                              >
                                {text}
                              </span>
                            ) : (
                              text
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-end gap-1 text-xs text-muted-foreground">
              <span aria-live="polite" className="mr-1 tabular-nums">
                {t("{from}–{to} of {total}", "{from}–{to} von {total}")
                  .replace("{from}", String(rangeOffset + 1))
                  .replace("{to}", String(rangeOffset + rows.length))
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
