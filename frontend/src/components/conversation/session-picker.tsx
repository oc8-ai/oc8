// The compact session switcher the dock and the My Work chat widget mount
// above their `Conversation`. The Copilot page uses the sidebar instead.
import { useState, type FormEvent } from "react";
import { ChevronDown, Pencil, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/hooks/use-confirm";
import { useDeleteChatSession, useRenameChatSession, type ChatSessionDTO } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";

import { ChatTitleText } from "./chat-title";
import { useNewChatLabel } from "./new-chat-label";

// A real menu (Radix dropdown semantics): every chat is a radio item, so the
// arrow keys move between them, Enter or Space opens one and Esc closes the
// menu back to its trigger. Rename and delete act on the open chat, from two
// items under the list; renaming happens in a short dialog, because an input
// inside a menu fights the menu's own keys (typeahead, Tab).
//
// The selection is always the caller's `sessionId` (the dock and widget seed
// theirs with `mostRecentSession`); this component never defaults to the
// list's first entry, which is a pinned chat when there is one.
export function SessionPicker({
  sessions,
  sessionId,
  onSelect,
}: {
  sessions: ChatSessionDTO[];
  sessionId: string | null;
  onSelect: (sessionId: string | null) => void;
}) {
  const t = useT();
  const rename = useRenameChatSession();
  const del = useDeleteChatSession();
  const { confirm, ConfirmDialog } = useConfirm();
  const [renaming, setRenaming] = useState<ChatSessionDTO | null>(null);
  const [title, setTitle] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // A chat without a title yet is waiting for the one named after its first
  // message, so it reads as new rather than as nameless.
  const untitled = useNewChatLabel();
  const current = sessions.find((s) => s.id === sessionId);

  function startRename(s: ChatSessionDTO) {
    setRenameError(null);
    setTitle(s.title);
    setRenaming(s);
  }

  function commitRename(e: FormEvent) {
    e.preventDefault();
    const s = renaming;
    const trimmed = title.trim();
    if (!s) return;
    if (!trimmed || trimmed === s.title) {
      setRenaming(null);
      return;
    }
    rename.mutate(
      { sessionId: s.id, title: trimmed },
      {
        onSuccess: () => setRenaming(null),
        onError: () =>
          setRenameError(
            t(
              "The chat was not renamed. Try again.",
              "Der Chat wurde nicht umbenannt. Versuch es noch einmal.",
            ),
          ),
      },
    );
  }

  async function handleDelete(s: ChatSessionDTO) {
    const label = s.title || untitled;
    setDeleteError(null);
    const ok = await confirm({
      title: t("Delete chat?", "Chat löschen?"),
      description: t(
        `This permanently deletes "${label}" and its messages.`,
        `Löscht „${label}“ und alle Nachrichten dauerhaft.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    del.mutate(s.id, {
      onSuccess: () => sessionId === s.id && onSelect(null),
      onError: () =>
        setDeleteError(
          t(
            "The chat was not deleted. Try again.",
            "Der Chat wurde nicht gelöscht. Versuch es noch einmal.",
          ),
        ),
    });
  }

  return (
    <>
      {ConfirmDialog}
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label={t("Switch chat", "Chat wechseln")}
          className="inline-flex h-9 max-w-[220px] items-center gap-1 rounded-md border border-border bg-background px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          {/* No chat selected is a new one, and so is one still untitled. */}
          <ChatTitleText
            className="truncate"
            title={current ? current.title || untitled : untitled}
          />
          <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72 p-1">
          <DropdownMenuRadioGroup value={sessionId ?? ""} onValueChange={(id) => onSelect(id)}>
            {sessions.map((s) => {
              const label = s.title || untitled;
              return (
                <DropdownMenuRadioItem
                  key={s.id}
                  value={s.id}
                  title={label}
                  className="min-h-11 gap-1.5"
                >
                  <ChatTitleText className="min-w-0 flex-1 truncate" title={label} />
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {new Date(s.createdAt).toLocaleDateString()}
                  </span>
                </DropdownMenuRadioItem>
              );
            })}
          </DropdownMenuRadioGroup>
          {current && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem className="min-h-11 gap-2" onSelect={() => startRename(current)}>
                <Pencil className="size-3.5" aria-hidden />
                {t("Rename this chat…", "Diesen Chat umbenennen …")}
              </DropdownMenuItem>
              <DropdownMenuItem
                className="min-h-11 gap-2 text-destructive focus:text-destructive"
                onSelect={() => void handleDelete(current)}
              >
                <Trash2 className="size-3.5" aria-hidden />
                {t("Delete this chat…", "Diesen Chat löschen …")}
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {deleteError && (
        <span role="alert" className="min-w-0 truncate text-xs text-status-error">
          {deleteError}
        </span>
      )}
      <Dialog open={!!renaming} onOpenChange={(open) => !open && setRenaming(null)}>
        <DialogContent className="sm:max-w-sm" aria-describedby={undefined}>
          <form onSubmit={commitRename} className="space-y-4">
            <DialogHeader>
              <DialogTitle>{t("Rename chat", "Chat umbenennen")}</DialogTitle>
            </DialogHeader>
            <Input
              autoFocus
              value={title}
              placeholder={untitled}
              aria-label={t("Chat title", "Chat-Titel")}
              onChange={(e) => setTitle(e.target.value)}
            />
            {renameError && (
              <p role="alert" className="text-xs text-status-error">
                {renameError}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setRenaming(null)}>
                {t("Cancel", "Abbrechen")}
              </Button>
              <Button type="submit" disabled={rename.isPending}>
                {t("Save title", "Titel speichern")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
