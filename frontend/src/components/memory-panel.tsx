import { BookOpen, Trash2 } from "lucide-react";
import { Panel } from "@/components/app-shell";
import { useT } from "@/lib/i18n";
import type { MemoryRecord } from "@/lib/hooks";

/** Shared list+delete view for the §10 memory tiers -- the agent detail
 * page's Memory tab (tier="agent") and the department page's Memory
 * section (tier="department") are the same UI over a different hook, so
 * this takes plain data + callbacks rather than fetching itself. */
export function MemoryPanel({
  records,
  isLoading,
  mayManage,
  onDelete,
  deletingId,
  emptyLabel,
}: {
  records: MemoryRecord[] | undefined;
  isLoading: boolean;
  mayManage: boolean;
  onDelete: (recordId: string) => void;
  deletingId: string | null;
  emptyLabel: string;
}) {
  const t = useT();

  if (isLoading) {
    return (
      <Panel className="p-10 text-center text-sm text-muted-foreground">
        {t("Loading…", "Wird geladen…")}
      </Panel>
    );
  }

  if (!records || records.length === 0) {
    return (
      <Panel className="p-10 text-center">
        <BookOpen className="mx-auto mb-3 h-6 w-6 text-muted-foreground/60" />
        <p className="text-sm text-muted-foreground">{emptyLabel}</p>
      </Panel>
    );
  }

  return (
    <Panel className="p-5">
      <div className="mb-3 text-xs uppercase tracking-wider text-muted-foreground">
        {records.length} {t("memories", "Erinnerungen")}
      </div>
      <ul className="space-y-2">
        {records.map((r) => (
          <li
            key={r.id}
            className="rounded-md border border-border/60 bg-background/30 p-3 text-sm"
          >
            <div className="flex items-start justify-between gap-3">
              <p className="min-w-0 flex-1 whitespace-pre-wrap text-foreground/90">{r.content}</p>
              {mayManage && (
                <button
                  type="button"
                  onClick={() => onDelete(r.id)}
                  disabled={deletingId === r.id}
                  title={t("Delete this memory", "Diese Erinnerung löschen")}
                  className="shrink-0 rounded-md p-1.5 text-muted-foreground transition hover:bg-status-error/10 hover:text-status-error disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            <div className="mt-2 text-xs text-muted-foreground">
              {new Date(r.createdAt).toLocaleString()}
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
