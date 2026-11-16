// New dashboards shared with the person, above their My Work grid: add one as
// a tile or dismiss it. Either way the notice is marked read; a dashboard that
// is already on the grid needs no notice, so that one is marked read silently.
import { LayoutDashboard, X } from "lucide-react";
import { useEffect } from "react";

import type { WidgetType } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { useMarkRead, useNotifications } from "@/lib/notifications-api";

const MAX_SHOWN = 3;

export function BoardNotices({
  onAdd,
  boardIdsOnGrid,
}: {
  onAdd: (type: WidgetType, config?: Record<string, unknown>) => void;
  boardIdsOnGrid: string[];
}) {
  const t = useT();
  const { data: notices = [] } = useNotifications({ unread: true, kind: "board.available" });
  const markRead = useMarkRead();
  const onGrid = new Set(boardIdsOnGrid);
  const pending = notices.filter((n) => n.subjectId && !onGrid.has(n.subjectId));
  const redundant = notices.filter((n) => n.subjectId && onGrid.has(n.subjectId));
  const redundantKey = redundant.map((n) => n.id).join(",");

  useEffect(() => {
    for (const id of redundantKey ? redundantKey.split(",") : []) markRead.mutate(id);
    // `markRead` is a new object each render; the ids are the real trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [redundantKey]);

  if (pending.length === 0) return null;
  const shown = pending.slice(0, MAX_SHOWN);
  const more = pending.length - shown.length;

  return (
    <div className="space-y-2">
      {shown.map((n) => (
        <div
          key={n.id}
          className="flex min-w-0 items-center gap-3 rounded-md border border-border bg-panel px-3 py-2"
        >
          <LayoutDashboard className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{n.title}</div>
            <div className="truncate text-xs text-muted-foreground">{n.body}</div>
          </div>
          <button
            type="button"
            aria-label={t("Add to My work", "Zu Meine Arbeit hinzufügen")}
            onClick={() => {
              onAdd("data_board", { boardId: n.subjectId });
              markRead.mutate(n.id);
            }}
            className="shrink-0 rounded-md border border-border px-2.5 py-1 text-xs transition hover:bg-muted/30"
          >
            {t("Add to My work", "Zu Meine Arbeit hinzufügen")}
          </button>
          <button
            type="button"
            aria-label={t("Dismiss", "Schließen")}
            onClick={() => markRead.mutate(n.id)}
            className="grid size-6 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
          >
            <X className="size-3.5" />
          </button>
        </div>
      ))}
      {more > 0 && (
        <div className="px-1 text-xs text-muted-foreground">
          {t(`+${more} more`, `+${more} weitere`)}
        </div>
      )}
    </div>
  );
}
