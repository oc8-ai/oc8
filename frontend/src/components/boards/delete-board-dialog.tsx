// Deleting a dashboard deletes everything its agent recorded for it, so the
// confirmation names the record count, and the request carries that count:
// the server refuses when more records arrived since (§6.8.6).
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { recordsChanged, useDeleteBoard, type BoardSummary } from "@/lib/boards-api";
import { useT } from "@/lib/i18n";

export function DeleteBoardDialog({
  board,
  open,
  onOpenChange,
}: {
  board: BoardSummary;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const remove = useDeleteBoard();
  if (!board.mayDelete) return null;
  const title = board.title;
  const n = board.recordCount;

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("Delete dashboard?", "Dashboard löschen?")}</AlertDialogTitle>
          <AlertDialogDescription>
            {t(
              `Delete "${title}" and its ${n} records?`,
              `„${title}" und seine ${n} Einträge löschen?`,
            )}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t("Cancel", "Abbrechen")}</AlertDialogCancel>
          <Button
            variant="destructive"
            disabled={remove.isPending}
            onClick={() =>
              remove.mutate(
                { id: board.id, records: n },
                {
                  onSuccess: () => onOpenChange(false),
                  onError: (error) =>
                    toast.error(
                      recordsChanged(error)
                        ? // The count above refreshes (`useDeleteBoard`); the
                          // dialog stays open for the person to confirm it.
                          t(
                            "New records arrived — check the count and confirm again.",
                            "Neue Einträge sind hinzugekommen — prüf die Anzahl und bestätige noch einmal.",
                          )
                        : t(
                            "The dashboard could not be deleted. Reload and try again.",
                            "Das Dashboard konnte nicht gelöscht werden. Lade neu und versuch es noch einmal.",
                          ),
                    ),
                },
              )
            }
          >
            {t("Delete", "Löschen")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
