import { WidgetTitle } from "@/components/dashboard/widget-title";
import { useBoardQuery } from "@/lib/boards-api";
import type { BoardQuery, DatasetSpec, NoteBlock as NoteSpec } from "@/lib/boards-api";
import { useLang, useT } from "@/lib/i18n";

import { BlockMessage, BlockStatus } from "./block-status";
import { formatAsOf } from "./board-format";

/** The latest note the agent wrote for the period, with the date it is for. */
export function NoteBlock({
  boardId,
  block,
  dataset,
  query,
}: {
  boardId: string;
  block: NoteSpec;
  dataset: DatasetSpec;
  query: BoardQuery | null;
}) {
  const t = useT();
  const { lang } = useLang();
  const { data, error } = useBoardQuery(boardId, query);
  const note = data?.note ?? null;

  return (
    <div className="flex h-full flex-col">
      <WidgetTitle title={block.title || t("Note", "Notiz")} />
      <BlockStatus data={data} error={error} t={t} />
      {data &&
        (note ? (
          <div className="flex flex-col gap-1 px-3 pb-3">
            <p className="whitespace-pre-wrap text-sm">{note.text}</p>
            <time dateTime={note.asOf} className="text-xs text-muted-foreground">
              {formatAsOf(note.asOf, dataset.grain, lang)}
            </time>
          </div>
        ) : (
          <BlockMessage>
            {t("No note for this period", "Keine Notiz für diesen Zeitraum")}
          </BlockMessage>
        ))}
    </div>
  );
}
