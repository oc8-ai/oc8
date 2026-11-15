// The dashboards an agent fills, on its page: each with its record count and,
// for a person who may, a delete that names that count first.
import { LayoutDashboard, Trash2 } from "lucide-react";
import { useState } from "react";

import { Panel } from "@/components/app-shell";
import { useBoards, type BoardSummary } from "@/lib/boards-api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { DeleteBoardDialog } from "./delete-board-dialog";

export function AgentBoardsSection({
  agentId,
  className,
}: {
  agentId: string;
  /** Placement in the page's grid. */
  className?: string;
}) {
  const t = useT();
  const { data: boards } = useBoards(agentId);
  const [deleting, setDeleting] = useState<BoardSummary | null>(null);
  if (!boards?.length) return null;

  return (
    <Panel className={cn("p-5", className)}>
      <div className="mb-2 text-xs uppercase tracking-wider text-muted-foreground">
        {t("Dashboards", "Dashboards")}
      </div>
      <ul>
        {boards.map((board) => (
          <li key={board.id} className="flex min-w-0 items-center gap-3 py-2">
            <LayoutDashboard className="size-4 shrink-0 text-muted-foreground" aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm">{board.title}</div>
              <div className="text-xs text-muted-foreground">
                {board.recordCount === 1
                  ? t("1 record", "1 Eintrag")
                  : t(`${board.recordCount} records`, `${board.recordCount} Einträge`)}
                {board.stale ? ` · ${t("No longer updated", "Wird nicht mehr aktualisiert")}` : ""}
              </div>
            </div>
            {board.mayDelete && (
              <button
                type="button"
                onClick={() => setDeleting(board)}
                aria-label={t(`Delete "${board.title}"`, `„${board.title}" löschen`)}
                className="grid size-8 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
              >
                <Trash2 className="size-4" />
              </button>
            )}
          </li>
        ))}
      </ul>
      {deleting && (
        <DeleteBoardDialog
          board={deleting}
          open
          onOpenChange={(open) => !open && setDeleting(null)}
        />
      )}
    </Panel>
  );
}
