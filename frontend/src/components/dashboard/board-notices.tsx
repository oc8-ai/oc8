// New dashboards shared with the person, above their My Work grid: add one as
// a tile or dismiss it. Either way the notice is marked read; a dashboard that
// is already on the grid needs no notice, so that one is marked read silently.
import { LayoutDashboard, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

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
  // Ids being acted on: a second click on the same notice is a no-op while the
  // read mark is in flight. A failed mark releases it, so the person can retry.
  const acting = useRef(new Set<string>());
  // Ids auto-marked (their board is on the grid): once per mount, never
  // retried, so a persistent failure cannot loop. The bell still shows them.
  const autoMarked = useRef(new Set<string>());
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const onGrid = new Set(boardIdsOnGrid);
  const open = notices.filter((n) => n.subjectId && !autoMarked.current.has(n.id));
  const pending = open.filter((n) => !onGrid.has(n.subjectId!) && !hidden.has(n.id));
  const redundant = open.filter((n) => onGrid.has(n.subjectId!));

  useEffect(() => {
    for (const n of redundant) {
      if (autoMarked.current.has(n.id)) continue;
      autoMarked.current.add(n.id);
      markRead.mutate(n.id);
    }
  });

  const handle = async (id: string, after?: () => void) => {
    if (acting.current.has(id)) return;
    acting.current.add(id);
    after?.();
    try {
      await markRead.mutateAsync(id);
      setHidden((prev) => new Set(prev).add(id));
    } catch {
      acting.current.delete(id);
    }
  };

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
            onClick={() => void handle(n.id, () => onAdd("data_board", { boardId: n.subjectId }))}
            className="shrink-0 rounded-md border border-border px-2.5 py-1 text-xs transition hover:bg-muted/30"
          >
            {t("Add to My work", "Zu Meine Arbeit hinzufügen")}
          </button>
          <button
            type="button"
            aria-label={t("Dismiss", "Schließen")}
            onClick={() => void handle(n.id)}
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
