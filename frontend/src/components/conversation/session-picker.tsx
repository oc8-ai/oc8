// The compact session switcher the dock and the My Work chat widget mount
// above their `Conversation`. The Copilot page uses the sidebar instead.
import { useState } from "react";
import { ChevronDown, Pencil, Trash2 } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useConfirm } from "@/hooks/use-confirm";
import { useDeleteChatSession, useRenameChatSession, type ChatSessionDTO } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// Session switcher + per-session rename/delete. A native <select> can't host
// per-row buttons, so this is a dropdown of plain rows instead of
// DropdownMenuItem: an Item's onSelect closes the whole menu, which would
// kill an in-progress rename or interrupt a delete confirmation.
//
// The selection is always the caller's `sessionId` (the dock and widget seed
// theirs with `mostRecentSession`); this component never defaults to the
// list's first entry, which is a pinned chat when there is one.
export function SessionPicker({
  sessions,
  sessionId,
  onSelect,
}: {
  agentId: string;
  sessions: ChatSessionDTO[];
  sessionId: string | null;
  onSelect: (sessionId: string | null) => void;
}) {
  const t = useT();
  const rename = useRenameChatSession();
  const del = useDeleteChatSession();
  const { confirm, ConfirmDialog } = useConfirm();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  const untitled = t("Untitled chat", "Unbenannter Chat");
  const current = sessions.find((s) => s.id === sessionId);

  function startRename(s: ChatSessionDTO) {
    setEditingId(s.id);
    setEditValue(s.title || untitled);
  }

  function commitRename(s: ChatSessionDTO) {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== s.title) rename.mutate({ sessionId: s.id, title: trimmed });
    setEditingId(null);
  }

  async function handleDelete(s: ChatSessionDTO) {
    const label = s.title || untitled;
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
    del.mutate(s.id, { onSuccess: () => sessionId === s.id && onSelect(null) });
  }

  return (
    <>
      {ConfirmDialog}
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label={t("Switch chat", "Chat wechseln")}
          className="inline-flex h-9 max-w-[220px] items-center gap-1 rounded-md border border-border bg-background px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <span className="truncate">{current?.title || untitled}</span>
          <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72 p-1">
          {sessions.map((s) => {
            const label = s.title || untitled;
            return (
              <div
                key={s.id}
                className={cn(
                  "group flex items-center gap-1 rounded-md px-1 text-sm",
                  s.id === sessionId && "bg-muted",
                )}
              >
                {editingId === s.id ? (
                  <input
                    autoFocus
                    value={editValue}
                    aria-label={t("Chat title", "Chat-Titel")}
                    onChange={(e) => setEditValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        commitRename(s);
                      } else if (e.key === "Escape") {
                        setEditingId(null);
                      }
                    }}
                    onBlur={() => commitRename(s)}
                    className="h-9 min-w-0 flex-1 rounded-md border border-primary/50 bg-background px-2 text-xs outline-none"
                  />
                ) : (
                  <button
                    type="button"
                    onClick={() => onSelect(s.id)}
                    className="min-h-9 flex-1 truncate px-2 text-left"
                    title={label}
                  >
                    {label}
                    <span className="ml-1.5 text-xs text-muted-foreground">
                      {new Date(s.createdAt).toLocaleDateString()}
                    </span>
                  </button>
                )}
                {editingId !== s.id && (
                  <div className="flex shrink-0 items-center opacity-0 transition group-focus-within:opacity-100 group-hover:opacity-100">
                    <button
                      type="button"
                      title={t("Rename", "Umbenennen")}
                      aria-label={t(`Rename ${label}`, `${label} umbenennen`)}
                      onClick={(e) => {
                        e.stopPropagation();
                        startRename(s);
                      }}
                      className="grid size-9 place-items-center rounded-md text-muted-foreground transition hover:text-foreground"
                    >
                      <Pencil className="size-3.5" />
                    </button>
                    <button
                      type="button"
                      title={t("Delete", "Löschen")}
                      aria-label={t(`Delete ${label}`, `${label} löschen`)}
                      onClick={(e) => {
                        e.stopPropagation();
                        void handleDelete(s);
                      }}
                      className="grid size-9 place-items-center rounded-md text-muted-foreground transition hover:text-destructive"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}
