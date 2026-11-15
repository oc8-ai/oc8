// A dashboard an agent fills, as a My Work tile. The tile stores only the
// board's id and the person's filters (`{boardId, filters}`); the board itself
// is read through `/boards`, so access is checked on every read: a person who
// lost access sees "No access" -- never the title -- on the next fetch.
import { Trash2 } from "lucide-react";
import { useState } from "react";

import { BlockMessage } from "@/components/boards/block-status";
import { DEFAULT_FILTERS, type BoardFilterState } from "@/components/boards/board-filters";
import { BoardView } from "@/components/boards/board-view";
import { DeleteBoardDialog } from "@/components/boards/delete-board-dialog";
import { WidgetTitle } from "@/components/dashboard/widget-title";
import { ApiError } from "@/lib/api";
import { useBoard, useBoardDeleted } from "@/lib/boards-api";
import { useT } from "@/lib/i18n";

function boardIdOf(config: Record<string, unknown>): string {
  return typeof config.boardId === "string" ? config.boardId : "";
}

/** The saved filters over the defaults, so a tile saved before a filter
 *  existed still gets a value for it. */
function filtersOf(config: Record<string, unknown>): BoardFilterState {
  const saved = config.filters;
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) return DEFAULT_FILTERS;
  const merged = { ...DEFAULT_FILTERS, ...(saved as Partial<BoardFilterState>) };
  const categories = merged.categories;
  return {
    ...merged,
    categories:
      categories && typeof categories === "object" && !Array.isArray(categories) ? categories : {},
  };
}

/** A state with no board to show: one centred line filling the tile. */
function TileMessage({ children }: { children: string }) {
  return (
    <div className="flex h-full flex-col">
      <BlockMessage>{children}</BlockMessage>
    </div>
  );
}

function statusOf(error: unknown): number | null {
  return error instanceof ApiError ? error.status : null;
}

export function DataBoardWidget({
  config,
  onConfigChange,
}: {
  config: Record<string, unknown>;
  onConfigChange: (config: Record<string, unknown>) => void;
}) {
  const t = useT();
  const boardId = boardIdOf(config);
  const deleted = useBoardDeleted(boardId);
  const { data: board, error } = useBoard(boardId);
  const status = statusOf(error);

  if (deleted || status === 410) {
    return (
      <TileMessage>
        {t("This dashboard was removed.", "Dieses Dashboard wurde entfernt.")}
      </TileMessage>
    );
  }
  // A tile without a usable board id is a broken config, not a refusal.
  if (!boardId) {
    return (
      <TileMessage>
        {t("This dashboard can't be shown.", "Dieses Dashboard kann nicht angezeigt werden.")}
      </TileMessage>
    );
  }
  // 404 is also what the server answers once the person lost access: say no
  // more than that, not even the title.
  if (status === 404 || status === 403) {
    return <TileMessage>{t("No access", "Kein Zugriff")}</TileMessage>;
  }
  if (!board) {
    return (
      <TileMessage>
        {error
          ? t("This dashboard couldn't be loaded.", "Dieses Dashboard konnte nicht geladen werden.")
          : t("Loading…", "Wird geladen…")}
      </TileMessage>
    );
  }

  return (
    <div className="flex h-full min-w-0 flex-col">
      <WidgetTitle title={board.title} />
      {board.stale && (
        <p className="shrink-0 px-3 pb-1 text-xs text-[color:var(--status-warning)]">
          {t("No longer updated", "Wird nicht mehr aktualisiert")}
        </p>
      )}
      {/* No side padding: each block pads itself (px-3) like the title, so
          block titles line up with the tile's. */}
      <div
        data-testid="board-body"
        className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto pb-3"
      >
        <BoardView
          board={board}
          filters={filtersOf(config)}
          onFiltersChange={(filters) => onConfigChange({ ...config, filters })}
        />
      </div>
    </div>
  );
}

/** The tile's edit-mode menu: "Delete dashboard", for a person who may. It
 *  reads the board itself, so the frame never fetches board data. */
export function DataBoardMenu({ config }: { config: Record<string, unknown> }) {
  const t = useT();
  const boardId = boardIdOf(config);
  const deleted = useBoardDeleted(boardId);
  const { data: board } = useBoard(boardId);
  const [open, setOpen] = useState(false);
  if (deleted || !board?.mayDelete) return null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={t("Delete dashboard", "Dashboard löschen")}
        title={t("Delete dashboard", "Dashboard löschen")}
        className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
      <DeleteBoardDialog board={board} open={open} onOpenChange={setOpen} />
    </>
  );
}
