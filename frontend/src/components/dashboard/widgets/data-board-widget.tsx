// A dashboard an agent fills, as a My Work tile. The tile stores only the
// board's id and the person's filters (`{boardId, filters}`); the board itself
// is read through `/boards`, so access is checked on every read: a person who
// lost access sees "No access" -- never the title -- on the next fetch.
import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Share2, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { BlockMessage } from "@/components/boards/block-status";
import { DEFAULT_FILTERS, type BoardFilterState } from "@/components/boards/board-filters";
import { BoardView } from "@/components/boards/board-view";
import { DeleteBoardDialog } from "@/components/boards/delete-board-dialog";
import { ShareBoardDialog } from "@/components/boards/share-board-dialog";
import { WidgetTitle } from "@/components/dashboard/widget-title";
import { ApiError } from "@/lib/api";
import { boardKeys, useBoard, useBoardDeleted } from "@/lib/boards-api";
import type { Board } from "@/lib/boards-api";
import { isTerminalRunState, useRun, useRunAgent } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

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
      <WidgetTitle
        title={board.title}
        actions={
          board.mayRun && board.agentId && !board.stale ? (
            <RefreshBoardButton board={board} agentId={board.agentId} />
          ) : undefined
        }
      />
      {board.stale && (
        <p className="shrink-0 px-3 pb-1 text-xs text-[color:var(--status-warning)]">
          {t("No longer updated", "Wird nicht mehr aktualisiert")}
        </p>
      )}
      {/* No side padding: the board pads its grid (px-3) like the title, so
          its cards line up with the tile's title. */}
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

/** Asks the board's agent for a run that refreshes it. Busy from the click
 *  until that run ends; then the board and its data are read again, and a
 *  run that did not finish says so. */
function RefreshBoardButton({ board, agentId }: { board: Board; agentId: string }) {
  const t = useT();
  const qc = useQueryClient();
  const runAgent = useRunAgent();
  const [runId, setRunId] = useState<string | null>(null);
  const state = useRun(runId).data?.state;
  const boardId = board.id;

  useEffect(() => {
    if (!runId || !state || !isTerminalRunState(state)) return;
    setRunId(null);
    // The board (its record count) and every query of its blocks: all of
    // them sit under `["boards", id]`. The lists show the count too.
    void qc.invalidateQueries({ queryKey: boardKeys.one(boardId) });
    void qc.invalidateQueries({ predicate: (q) => q.meta?.boardList === true });
    if (state !== "done") toast.error(t("Couldn't refresh", "Aktualisierung fehlgeschlagen"));
  }, [runId, state, boardId, qc, t]);

  const busy = runAgent.isPending || runId !== null;
  const label = t("Refresh data", "Daten aktualisieren");
  return (
    <button
      type="button"
      disabled={busy}
      aria-busy={busy}
      onClick={() =>
        runAgent.mutate(
          {
            agentId,
            task: t(
              `Refresh the dashboard "${board.title}" with current data.`,
              `Aktualisiere das Dashboard „${board.title}“ mit aktuellen Daten.`,
            ),
          },
          {
            onSuccess: (run) => setRunId(run.id),
            onError: () => toast.error(t("Couldn't refresh", "Aktualisierung fehlgeschlagen")),
          },
        )
      }
      aria-label={label}
      title={busy ? t("Updating…", "Wird aktualisiert …") : label}
      className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-muted/40 hover:text-foreground disabled:cursor-default disabled:hover:bg-transparent"
    >
      <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} />
    </button>
  );
}

/** The tile's edit-mode menu: "Share dashboard" and "Delete dashboard", each
 *  for a person who may, and a "Shared with you" label when the dashboard
 *  reached them through a share. It reads the board itself, so the frame never
 *  fetches board data. */
export function DataBoardMenu({ config }: { config: Record<string, unknown> }) {
  const t = useT();
  const boardId = boardIdOf(config);
  const deleted = useBoardDeleted(boardId);
  const { data: board } = useBoard(boardId);
  const [open, setOpen] = useState(false);
  const [sharing, setSharing] = useState(false);
  if (deleted || !board) return null;
  if (!board.mayDelete && !board.mayShare && !board.sharedWithMe) return null;

  return (
    <>
      {board.sharedWithMe && (
        <span className="shrink-0 text-[10px] text-muted-foreground">
          {t("Shared with you", "Mit dir geteilt")}
        </span>
      )}
      {board.mayShare && (
        <>
          <button
            type="button"
            onClick={() => setSharing(true)}
            aria-label={t("Share dashboard", "Dashboard teilen")}
            title={t("Share dashboard", "Dashboard teilen")}
            className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
          >
            <Share2 className="h-3.5 w-3.5" />
          </button>
          <ShareBoardDialog board={board} open={sharing} onOpenChange={setSharing} />
        </>
      )}
      {board.mayDelete && (
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
      )}
    </>
  );
}
