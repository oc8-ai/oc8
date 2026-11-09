// frontend/src/components/conversation/project-sheet.tsx
// Create or change a chat project: a name, instructions every chat in it
// follows, and pinned context. Opens as a sheet (bottom under 640 px).
import { useId, useState } from "react";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import {
  useSaveProject,
  type ChatProjectDTO,
  type ContextRefViewDTO,
} from "@/lib/conversation-api";
import { useIsNarrow } from "@/hooks/use-mobile";
import { useT } from "@/lib/i18n";

import { ContextSearchDialog } from "./context-search";
import { PinnedBar } from "./pinned-bar";

const refKey = (r: { kind: string; id: string }) => `${r.kind}:${r.id}`;

export function ProjectSheet({
  project,
  onClose,
}: {
  /** The project to change; null creates one. */
  project: ChatProjectDTO | null;
  onClose: () => void;
}) {
  const t = useT();
  const narrow = useIsNarrow();
  const nameId = useId();
  const instructionsId = useId();
  const save = useSaveProject();
  const [name, setName] = useState(project?.name ?? "");
  const [instructions, setInstructions] = useState(project?.instructions ?? "");
  const [refs, setRefs] = useState<ContextRefViewDTO[]>(project?.pinnedRefs ?? []);
  const [picking, setPicking] = useState(false);
  const trimmed = name.trim();

  function submit() {
    if (!trimmed || save.isPending) return;
    // A change sends only what changed: pins the member can no longer see stay.
    const refsChanged = refs.map(refKey).join() !== (project?.pinnedRefs ?? []).map(refKey).join();
    save.mutate(
      {
        id: project?.id,
        name: trimmed,
        instructions: !project || instructions !== project.instructions ? instructions : undefined,
        pinnedRefs:
          !project || refsChanged ? refs.map(({ kind, id }) => ({ kind, id })) : undefined,
      },
      { onSuccess: onClose },
    );
  }

  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent
        side={narrow ? "bottom" : "right"}
        className={
          narrow ? "flex h-[90vh] flex-col gap-4 p-5" : "flex flex-col gap-4 p-5 sm:max-w-md"
        }
      >
        <SheetHeader>
          <SheetTitle>
            {project ? t("Edit project", "Projekt bearbeiten") : t("New project", "Neues Projekt")}
          </SheetTitle>
          <SheetDescription>
            {t(
              "Chats in a project follow its instructions and see its pinned context.",
              "Chats in einem Projekt befolgen seine Anweisungen und sehen das angeheftete Material.",
            )}
          </SheetDescription>
        </SheetHeader>
        <form
          className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div className="space-y-1">
            <label htmlFor={nameId} className="text-sm font-medium">
              {t("Name", "Name")}
            </label>
            <Input
              id={nameId}
              value={name}
              maxLength={120}
              autoFocus
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor={instructionsId} className="text-sm font-medium">
              {t("Instructions", "Anweisungen")}
            </label>
            <Textarea
              id={instructionsId}
              value={instructions}
              maxLength={8000}
              rows={5}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder={t(
                "How should I work on chats in this project?",
                "Wie soll ich bei Chats in diesem Projekt vorgehen?",
              )}
            />
          </div>
          <div className="space-y-2">
            <PinnedBar
              pins={refs}
              pinning={{
                isPending: false,
                mutate: (change) => {
                  if ("remove" in change)
                    setRefs((prev) => prev.filter((r) => refKey(r) !== refKey(change.remove)));
                },
              }}
            />
            <Button type="button" variant="ghost" className="h-9" onClick={() => setPicking(true)}>
              <Plus aria-hidden />
              {t("Pin context", "Material anheften")}
            </Button>
          </div>
          {save.isError && (
            <p role="alert" className="text-sm text-destructive">
              {t(
                "The project could not be saved. Check the name and try again.",
                "Das Projekt konnte nicht gespeichert werden. Prüf den Namen und versuch es noch einmal.",
              )}
            </p>
          )}
          <SheetFooter className="mt-auto gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              {t("Cancel", "Abbrechen")}
            </Button>
            <Button type="submit" disabled={!trimmed || save.isPending}>
              {project ? t("Save", "Speichern") : t("Create project", "Projekt anlegen")}
            </Button>
          </SheetFooter>
        </form>
        <ContextSearchDialog
          open={picking}
          onClose={() => setPicking(false)}
          onPick={(item) => {
            setRefs((prev) =>
              prev.some((r) => refKey(r) === refKey(item))
                ? prev
                : [...prev, { kind: item.kind, id: item.id, label: item.label, available: true }],
            );
            setPicking(false);
          }}
        />
      </SheetContent>
    </Sheet>
  );
}
