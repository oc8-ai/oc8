// frontend/src/components/conversation/copilot-sidebar.tsx
// The Copilot's chat list, grouped by pinned, project and date, with rename,
// pin, move and delete on every row. Full-text search is an Enterprise
// section (chatSidebarSections), not part of core. Under 640 px the list
// opens from a button, in a sheet.
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import {
  FolderPlus,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  MessageSquare,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useChatSlot } from "@/edition/composition";
import { SlotBoundary } from "@/edition/slots";
import { useConfirm } from "@/hooks/use-confirm";
import { useIsNarrow } from "@/hooks/use-mobile";
import { mostRecentSession } from "@/lib/chat-sessions";
import {
  useChatProjects,
  useDeleteProject,
  useSetSessionPinned,
  useSetSessionProject,
  type ChatProjectDTO,
} from "@/lib/conversation-api";
import {
  chatKeys,
  useChatSessions,
  useDeleteChatSession,
  useRenameChatSession,
  type ChatSessionDTO,
} from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { ProjectSheet } from "./project-sheet";

export interface SessionGroups {
  pinned: ChatSessionDTO[];
  byProject: Map<string, ChatSessionDTO[]>;
  today: ChatSessionDTO[];
  week: ChatSessionDTO[];
  older: ChatSessionDTO[];
}

const activityAt = (s: ChatSessionDTO) => new Date(s.lastMessageAt ?? s.createdAt).getTime() || 0;

/** Pinned first, then a project, then by date. "Today" is the member's local
 *  calendar day, "Previous 7 days" the six calendar days before it. Every
 *  group is newest first. */
// eslint-disable-next-line react-refresh/only-export-components -- the grouping is the sidebar's contract and is tested with it
export function groupSessions(sessions: ChatSessionDTO[], now: Date): SessionGroups {
  const groups: SessionGroups = {
    pinned: [],
    byProject: new Map(),
    today: [],
    week: [],
    older: [],
  };
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfWeek = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6).getTime();
  for (const s of [...sessions].sort((a, b) => activityAt(b) - activityAt(a))) {
    if (s.pinned) {
      groups.pinned.push(s);
    } else if (s.projectId) {
      const inProject = groups.byProject.get(s.projectId);
      if (inProject) inProject.push(s);
      else groups.byProject.set(s.projectId, [s]);
    } else {
      const at = activityAt(s);
      if (at >= startOfToday) groups.today.push(s);
      else if (at >= startOfWeek) groups.week.push(s);
      else groups.older.push(s);
    }
  }
  return groups;
}

const NO_SESSIONS: ChatSessionDTO[] = [];

interface RowActions {
  projects: ChatProjectDTO[];
  rename: (s: ChatSessionDTO, title: string) => void;
  pin: (s: ChatSessionDTO) => void;
  move: (s: ChatSessionDTO, projectId: string | null) => void;
  remove: (s: ChatSessionDTO) => void;
}

const itemClass = "min-h-11 cursor-pointer text-sm sm:min-h-9";
const triggerClass =
  "grid size-9 max-sm:size-11 shrink-0 cursor-pointer place-items-center rounded-md text-muted-foreground hover:bg-accent focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring data-[state=open]:opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100";

function Row({
  s,
  active,
  onSelect,
  actions,
}: {
  s: ChatSessionDTO;
  active: boolean;
  onSelect: (id: string) => void;
  actions: RowActions;
}) {
  const t = useT();
  const title = s.title || t("Untitled chat", "Unbenannter Chat");
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);
  // Escape ends the edit, and the blur that follows must not save it.
  const cancelled = useRef(false);
  // The menu is closing: the field opens once its focus return is done.
  const renameNext = useRef(false);
  // Radix hands focus back to the menu trigger as it closes; a blur in the
  // field's first frame is that, not the member leaving, so it is ignored.
  const armed = useRef(false);
  useEffect(() => {
    if (!editing) return;
    armed.current = false;
    const frame = requestAnimationFrame(() => {
      armed.current = true;
    });
    return () => cancelAnimationFrame(frame);
  }, [editing]);

  function commit() {
    const next = value.trim();
    if (!cancelled.current && next && next !== title) actions.rename(s, next);
    setEditing(false);
  }

  if (editing) {
    return (
      <li>
        <input
          autoFocus
          aria-label={t("Chat name", "Chat-Name")}
          value={value}
          maxLength={200}
          onChange={(e) => setValue(e.target.value)}
          onFocus={(e) => e.currentTarget.select()}
          onBlur={(e) => {
            if (!armed.current) {
              e.currentTarget.focus();
              return;
            }
            commit();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
            if (e.key === "Escape") {
              cancelled.current = true;
              e.currentTarget.blur();
            }
          }}
          className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </li>
    );
  }

  return (
    <li className="group">
      <div className={cn("flex items-center rounded-md hover:bg-muted", active && "bg-muted")}>
        <button
          type="button"
          onClick={() => onSelect(s.id)}
          aria-current={active ? "true" : undefined}
          className="min-h-9 max-sm:min-h-11 min-w-0 flex-1 truncate rounded-md px-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {title}
        </button>
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={t(`More: ${title}`, `Mehr: ${title}`)}
            className={triggerClass}
          >
            <MoreHorizontal className="size-4" aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="w-56 p-1"
            onCloseAutoFocus={(e) => {
              if (!renameNext.current) return;
              renameNext.current = false;
              e.preventDefault();
              cancelled.current = false;
              setValue(title);
              setEditing(true);
            }}
          >
            <DropdownMenuItem
              className={itemClass}
              onSelect={() => {
                renameNext.current = true;
              }}
            >
              {t("Rename", "Umbenennen")}
            </DropdownMenuItem>
            <DropdownMenuItem className={itemClass} onSelect={() => actions.pin(s)}>
              {s.pinned ? t("Unpin", "Lösen") : t("Pin", "Anheften")}
            </DropdownMenuItem>
            {(actions.projects.length > 0 || s.projectId) && <DropdownMenuSeparator />}
            {actions.projects.length > 0 && (
              <DropdownMenuLabel className="text-micro px-2 py-1">
                {t("Move to project", "In Projekt verschieben")}
              </DropdownMenuLabel>
            )}
            {actions.projects.map((p) => (
              <DropdownMenuItem
                key={p.id}
                className={itemClass}
                disabled={p.id === s.projectId}
                onSelect={() => actions.move(s, p.id)}
              >
                <span className="truncate">
                  {t(`Move to ${p.name}`, `Nach ${p.name} verschieben`)}
                </span>
              </DropdownMenuItem>
            ))}
            {s.projectId && (
              <DropdownMenuItem className={itemClass} onSelect={() => actions.move(s, null)}>
                {t("Remove from project", "Aus dem Projekt nehmen")}
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className={cn(itemClass, "text-destructive focus:text-destructive")}
              onSelect={() => actions.remove(s)}
            >
              {t("Delete", "Löschen")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </li>
  );
}

function Group({
  title,
  sessions,
  headingExtra,
  empty,
  level = 2,
  ...rest
}: {
  title: string;
  sessions: ChatSessionDTO[];
  /** Beside the heading, e.g. the project's menu. */
  headingExtra?: ReactNode;
  /** Shown when the group has no chats; the group is hidden when left out. */
  empty?: string;
  level?: 2 | 3;
  sessionId: string | null;
  onSelect: (id: string) => void;
  actions: RowActions;
}) {
  const headingId = useId();
  if (sessions.length === 0 && !empty) return null;
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <div>
      <div className="flex min-h-9 items-center justify-between gap-1 px-2">
        <Heading id={headingId} className="text-micro min-w-0 truncate">
          {title}
        </Heading>
        {headingExtra}
      </div>
      {sessions.length === 0 ? (
        <p className="px-2 pb-1 text-xs text-muted-foreground">{empty}</p>
      ) : (
        <ul aria-labelledby={headingId} className="space-y-0.5">
          {sessions.map((s) => (
            <Row
              key={s.id}
              s={s}
              active={s.id === rest.sessionId}
              onSelect={rest.onSelect}
              actions={rest.actions}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="space-y-2 p-2" aria-busy="true">
      {[0, 1, 2, 3, 4].map((i) => (
        <Skeleton key={i} className="h-9 w-full rounded-md" />
      ))}
    </div>
  );
}

interface SidebarProps {
  agentId: string;
  sessionId: string | null;
  /** A chat to open; null starts a new one. */
  onSelect: (id: string | null) => void;
  collapsed: boolean;
  onToggle: () => void;
}

export function CopilotSidebar({
  agentId,
  sessionId,
  onSelect,
  collapsed,
  onToggle,
}: SidebarProps) {
  const t = useT();
  const projectsHeadingId = useId();
  const narrow = useIsNarrow();
  const [sheetOpen, setSheetOpen] = useState(false);
  const [editingProject, setEditingProject] = useState<{ project: ChatProjectDTO | null } | null>(
    null,
  );
  const qc = useQueryClient();
  const { data: sessions = NO_SESSIONS, isLoading } = useChatSessions(agentId || undefined);
  const { data: projects = [] } = useChatProjects();
  const sections = useChatSlot("chatSidebarSections");
  const renameSession = useRenameChatSession();
  const deleteSession = useDeleteChatSession();
  const pinSession = useSetSessionPinned();
  const moveSession = useSetSessionProject();
  const deleteProject = useDeleteProject();
  const { confirm, ConfirmDialog } = useConfirm();
  // Regroup when the list changes or the local day turns over, not on every render.
  const localDay = new Date().toDateString();
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `localDay` stands for `now`
  const groups = useMemo(() => groupSessions(sessions, new Date()), [sessions, localDay]);

  function open(id: string | null) {
    onSelect(id);
    setSheetOpen(false);
  }

  const failed = {
    delete: t(
      "The chat was not deleted. Try again.",
      "Der Chat wurde nicht gelöscht. Versuch es noch einmal.",
    ),
    rename: t(
      "The chat was not renamed. Try again.",
      "Der Chat wurde nicht umbenannt. Versuch es noch einmal.",
    ),
    pin: t(
      "The chat was not changed. Try again.",
      "Der Chat wurde nicht geändert. Versuch es noch einmal.",
    ),
    move: t(
      "The chat was not moved. Try again.",
      "Der Chat wurde nicht verschoben. Versuch es noch einmal.",
    ),
    deleteProject: t(
      "The project was not deleted. Try again.",
      "Das Projekt wurde nicht gelöscht. Versuch es noch einmal.",
    ),
  };

  async function removeSession(s: ChatSessionDTO) {
    const title = s.title || t("Untitled chat", "Unbenannter Chat");
    const ok = await confirm({
      title: t("Delete chat?", "Chat löschen?"),
      description: t(
        `This permanently deletes "${title}" and its messages.`,
        `Löscht „${title}“ und alle Nachrichten dauerhaft.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    deleteSession.mutate(s.id, {
      onError: () => toast.error(failed.delete),
      onSuccess: () => {
        if (s.id !== sessionId) return;
        // The next most recent chat, whatever is pinned; a new chat if none.
        const latest =
          qc.getQueryData<ChatSessionDTO[]>(chatKeys.sessions(agentId || undefined)) ?? sessions;
        const next = mostRecentSession(latest.filter((x) => x.id !== s.id))?.id ?? null;
        onSelect(next);
      },
    });
  }

  async function removeProject(p: ChatProjectDTO) {
    const ok = await confirm({
      title: t("Delete project?", "Projekt löschen?"),
      description: t(
        `"${p.name}" is deleted. Its chats are kept and move back into the list by date.`,
        `„${p.name}“ wird gelöscht. Die Chats bleiben erhalten und stehen wieder nach Datum in der Liste.`,
      ),
      confirmLabel: t("Delete project", "Projekt löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (ok) deleteProject.mutate(p.id, { onError: () => toast.error(failed.deleteProject) });
  }

  const actions: RowActions = {
    projects,
    rename: (s, title) =>
      renameSession.mutate(
        { sessionId: s.id, title },
        { onError: () => toast.error(failed.rename) },
      ),
    pin: (s) =>
      pinSession.mutate(
        { sessionId: s.id, pinned: !s.pinned },
        { onError: () => toast.error(failed.pin) },
      ),
    move: (s, projectId) =>
      moveSession.mutate(
        { sessionId: s.id, projectId },
        { onError: () => toast.error(failed.move) },
      ),
    remove: (s) => void removeSession(s),
  };
  const shared = { sessionId, onSelect: (id: string) => open(id), actions };

  const newChat = (
    <Button
      type="button"
      variant="outline"
      className="h-9 flex-1 justify-start"
      onClick={() => open(null)}
    >
      <Plus aria-hidden />
      {t("New chat", "Neuer Chat")}
    </Button>
  );

  const list = (
    <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-2">
      {isLoading ? (
        <ListSkeleton />
      ) : (
        <>
          {sessions.length === 0 && projects.length === 0 && (
            <p className="px-2 text-sm text-muted-foreground">
              {t(
                "No chats yet. Start one with New chat.",
                "Noch keine Chats. Starte einen mit Neuer Chat.",
              )}
            </p>
          )}
          <Group title={t("Pinned", "Angeheftet")} sessions={groups.pinned} {...shared} />
          <section aria-labelledby={projectsHeadingId} className="space-y-1">
            <h2 id={projectsHeadingId} className="text-micro px-2">
              {t("Projects", "Projekte")}
            </h2>
            {projects.map((p) => (
              <Group
                key={p.id}
                level={3}
                title={p.name}
                sessions={groups.byProject.get(p.id) ?? []}
                empty={t("No chats in this project yet.", "Noch keine Chats in diesem Projekt.")}
                headingExtra={
                  <DropdownMenu>
                    <DropdownMenuTrigger
                      aria-label={t(`Project options: ${p.name}`, `Projekt-Optionen: ${p.name}`)}
                      className={cn(triggerClass, "sm:opacity-100")}
                    >
                      <MoreHorizontal className="size-4" aria-hidden />
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-52 p-1">
                      <DropdownMenuItem
                        className={itemClass}
                        onSelect={() => setEditingProject({ project: p })}
                      >
                        {t("Edit project", "Projekt bearbeiten")}
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        className={cn(itemClass, "text-destructive focus:text-destructive")}
                        onSelect={() => void removeProject(p)}
                      >
                        {t("Delete project", "Projekt löschen")}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                }
                {...shared}
              />
            ))}
            <Button
              type="button"
              variant="ghost"
              className="h-9 w-full justify-start"
              onClick={() => setEditingProject({ project: null })}
            >
              <FolderPlus aria-hidden />
              {t("New project", "Neues Projekt")}
            </Button>
          </section>
          <Group title={t("Today", "Heute")} sessions={groups.today} {...shared} />
          <Group title={t("Previous 7 days", "Letzte 7 Tage")} sessions={groups.week} {...shared} />
          <Group title={t("Older", "Ältere")} sessions={groups.older} {...shared} />
          {sections.map(({ id, component: Extra }) => (
            <SlotBoundary key={id} id={id}>
              <Extra agentId={agentId} onOpenSession={(sid) => open(sid)} />
            </SlotBoundary>
          ))}
        </>
      )}
    </div>
  );

  const dialogs = (
    <>
      {ConfirmDialog}
      {editingProject && (
        <ProjectSheet project={editingProject.project} onClose={() => setEditingProject(null)} />
      )}
    </>
  );

  if (narrow) {
    return (
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
          <SheetTrigger asChild>
            <Button type="button" variant="outline" className="h-9">
              <MessageSquare aria-hidden />
              {t("Chats", "Chats")}
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="flex w-4/5 max-w-sm flex-col gap-0 p-0">
            <SheetTitle className="sr-only">{t("Chats", "Chats")}</SheetTitle>
            <SheetDescription className="sr-only">
              {t("Your chats by date and project", "Deine Chats nach Datum und Projekt")}
            </SheetDescription>
            <nav aria-label={t("Chats", "Chats")} className="flex min-h-0 flex-1 flex-col pt-10">
              <div className="flex p-2">{newChat}</div>
              {list}
            </nav>
          </SheetContent>
        </Sheet>
        {dialogs}
      </div>
    );
  }

  return (
    <nav
      aria-label={t("Chats", "Chats")}
      className={cn(
        "flex shrink-0 flex-col border-r border-border bg-background",
        collapsed ? "w-14" : "w-64",
      )}
    >
      <div className="flex items-center gap-1 p-2">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-label={
            collapsed ? t("Show chats", "Chats einblenden") : t("Hide chats", "Chats ausblenden")
          }
          className="shrink-0"
        >
          {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
        </Button>
        {!collapsed && newChat}
      </div>
      {!collapsed && list}
      {dialogs}
    </nav>
  );
}
