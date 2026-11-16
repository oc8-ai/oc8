// Share a dashboard with a person or a whole department. A share grants read
// and query access to this one dashboard and nothing else.
import { Building2, User, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  useBoardShares,
  useShareBoard,
  useShareTargets,
  useUnshareBoard,
  type BoardShare,
  type BoardSummary,
} from "@/lib/boards-api";
import { useT } from "@/lib/i18n";

export function ShareBoardDialog({
  board,
  open,
  onOpenChange,
}: {
  board: BoardSummary;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const [q, setQ] = useState("");
  const { data: shares = [] } = useBoardShares(board.id, open);
  const { data: targets } = useShareTargets(board.id, q, open);
  const share = useShareBoard();
  const unshare = useUnshareBoard();

  const sharedMembers = new Set(shares.map((s) => s.memberId));
  const sharedDepartments = new Set(shares.map((s) => s.departmentId));
  const members = (targets?.members ?? []).filter((m) => !sharedMembers.has(m.id));
  const departments = (targets?.departments ?? []).filter((d) => !sharedDepartments.has(d.id));

  const failed = () =>
    toast.error(t("Couldn't share the dashboard", "Das Dashboard konnte nicht geteilt werden"));

  const add = (target: { memberId: string } | { departmentId: string }) =>
    share.mutate({ id: board.id, ...target }, { onError: failed });

  const remove = (s: BoardShare) =>
    unshare.mutate(
      { id: board.id, shareId: s.id },
      {
        onError: () =>
          toast.error(t("Couldn't remove the share", "Die Freigabe konnte nicht entfernt werden")),
      },
    );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t("Share dashboard", "Dashboard teilen")}</DialogTitle>
          <DialogDescription>
            {t(
              `People you add can see "${board.title}" and its data, nothing else.`,
              `Wen du hinzufügst, kann „${board.title}" und seine Daten sehen, sonst nichts.`,
            )}
          </DialogDescription>
        </DialogHeader>
        <Command shouldFilter={false} className="rounded-md border border-border">
          <CommandInput
            value={q}
            onValueChange={setQ}
            placeholder={t("Search people and departments", "Personen und Abteilungen suchen")}
          />
          <CommandList>
            <CommandEmpty>{t("No matches", "Keine Treffer")}</CommandEmpty>
            {members.length > 0 && (
              <CommandGroup heading={t("People", "Personen")}>
                {members.map((m) => (
                  <CommandItem
                    key={m.id}
                    value={`member-${m.id}`}
                    onSelect={() => add({ memberId: m.id })}
                  >
                    <User className="mr-2 size-4 text-muted-foreground" aria-hidden />
                    {m.name}
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {departments.length > 0 && (
              <CommandGroup heading={t("Departments", "Abteilungen")}>
                {departments.map((d) => (
                  <CommandItem
                    key={d.id}
                    value={`department-${d.id}`}
                    onSelect={() => add({ departmentId: d.id })}
                  >
                    <Building2 className="mr-2 size-4 text-muted-foreground" aria-hidden />
                    {d.name}
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
        <div>
          <div className="mb-1 text-xs uppercase tracking-wider text-muted-foreground">
            {t("Shared with", "Geteilt mit")}
          </div>
          {shares.length === 0 ? (
            <div className="text-xs text-muted-foreground">
              {t("Not shared yet", "Noch nicht geteilt")}
            </div>
          ) : (
            <ul>
              {shares.map((s) => {
                const name = s.memberName ?? s.departmentName ?? "";
                return (
                  <li key={s.id} className="flex items-center gap-2 py-1 text-sm">
                    {s.memberId ? (
                      <User className="size-4 text-muted-foreground" aria-hidden />
                    ) : (
                      <Building2 className="size-4 text-muted-foreground" aria-hidden />
                    )}
                    <span className="min-w-0 flex-1 truncate">{name}</span>
                    <button
                      type="button"
                      onClick={() => remove(s)}
                      aria-label={t(`Remove ${name}`, `${name} entfernen`)}
                      className="grid size-6 place-items-center rounded text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
                    >
                      <X className="size-3.5" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
