import { useMemo, useState } from "react";
import { toast } from "sonner";
import { AlertTriangle, ListTodo, Loader2, Plus } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError } from "@/lib/api";
import { useCreateTask, useDepartments, useTaskBoard, type TaskBoardRow } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// Same four columns as the department detail page's own board
// (`departments.$id.tsx`'s `COLUMNS`/`TaskCard`) — this widget is that board
// widened across every department the caller can see, so a task's column
// must mean the same thing in both places. Kept as a second literal rather
// than a shared import because the department page's version is entangled
// with mock-derived `Task`/`Agent` types this widget doesn't use.
type Column = "backlog" | "in_progress" | "waiting" | "done";

function columns(t: (en: string, de: string) => string): { id: Column; label: string }[] {
  return [
    { id: "backlog", label: t("Backlog", "Backlog") },
    { id: "in_progress", label: t("In Progress", "In Bearbeitung") },
    { id: "waiting", label: t("Waiting", "Wartet") },
    { id: "done", label: t("Done", "Erledigt") },
  ];
}

function relativeAge(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 60) return `${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.round(hrs / 24)}d`;
}

export function TaskBoardWidget(_props: {
  config: Record<string, unknown>;
  onConfigChange: (config: Record<string, unknown>) => void;
}) {
  const t = useT();
  const boardQuery = useTaskBoard();
  const rows = useMemo(() => boardQuery.data ?? [], [boardQuery.data]);
  const [creating, setCreating] = useState(false);

  const byColumn = useMemo(() => {
    const map = new Map<Column, TaskBoardRow[]>();
    for (const row of rows) {
      const list = map.get(row.column) ?? [];
      list.push(row);
      map.set(row.column, list);
    }
    return map;
  }, [rows]);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-xs text-muted-foreground">
          {rows.length} {t("tasks", "Aufgaben")}
        </span>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground transition hover:brightness-110 glow-teal"
        >
          <Plus className="h-3.5 w-3.5" /> {t("New task", "Neue Aufgabe")}
        </button>
      </div>

      {boardQuery.error && (
        <div className="flex items-start gap-2 border-b border-[color:var(--status-error)]/40 px-3 py-2 text-xs">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[color:var(--status-error)]" />
          <div>
            <div>
              {t(
                "The task board could not be loaded",
                "Das Aufgaben-Board konnte nicht geladen werden",
              )}
            </div>
            <div className="mt-0.5 text-muted-foreground">
              {boardQuery.error instanceof Error
                ? boardQuery.error.message
                : String(boardQuery.error)}
            </div>
          </div>
        </div>
      )}

      {!boardQuery.error && rows.length === 0 && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 p-4 text-center">
          <ListTodo className="h-5 w-5 text-primary" />
          <div className="text-sm">
            {boardQuery.isPending
              ? t("Loading…", "Wird geladen…")
              : t("No tasks yet — start one below.", "Noch keine Aufgaben — leg unten eine an.")}
          </div>
        </div>
      )}

      {rows.length > 0 && (
        <div className="flex-1 overflow-auto p-2">
          <div className="grid min-w-[560px] grid-cols-4 gap-2">
            {columns(t).map((col) => {
              const items = byColumn.get(col.id) ?? [];
              const isWaiting = col.id === "waiting";
              return (
                <div
                  key={col.id}
                  className={cn(
                    "flex min-h-[140px] flex-col rounded-lg border bg-panel/60 p-2",
                    isWaiting
                      ? "border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/5"
                      : "border-border",
                  )}
                >
                  <div className="mb-1.5 flex items-center justify-between">
                    <span
                      className={cn(
                        "micro-label",
                        isWaiting ? "text-[color:var(--status-warning)]" : "text-muted-foreground",
                      )}
                    >
                      {col.label}
                    </span>
                    <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 font-mono text-micro text-muted-foreground">
                      {items.length}
                    </span>
                  </div>
                  <div className="flex-1 space-y-1.5">
                    {items.map((row) => (
                      <TaskCard key={row.id} row={row} />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <NewTaskDialog open={creating} onOpenChange={setCreating} />
    </div>
  );
}

function TaskCard({ row }: { row: TaskBoardRow }) {
  const age = relativeAge(row.createdAt);
  const isDone = row.column === "done";
  return (
    <div
      className={cn(
        "rounded-md border border-border bg-background/40 p-2 text-xs",
        isDone && "opacity-70",
      )}
    >
      <div className={cn("leading-snug", isDone && "line-through")}>{row.title}</div>
      <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
        <span className="truncate">{row.departmentName}</span>
        {row.agentName && (
          <>
            <span>·</span>
            <span className="truncate">{row.agentName}</span>
          </>
        )}
        {age && (
          <>
            <span>·</span>
            <span>{age}</span>
          </>
        )}
      </div>
    </div>
  );
}

function NewTaskDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const departmentsQuery = useDepartments({ pageSize: 100 });
  const departments = departmentsQuery.data?.items ?? [];
  const createTask = useCreateTask();

  const [departmentId, setDepartmentId] = useState("");
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");

  // The picker has no selection to fall back on until the list loads, so the
  // first render after that has to set one -- otherwise submit is blocked
  // forever behind an empty string nothing in the UI explains.
  if (!departmentId && departments.length > 0) {
    setDepartmentId(departments[0].id);
  }

  function reset() {
    setTitle("");
    setInstructions("");
  }

  async function submit() {
    if (!departmentId || instructions.trim().length === 0) return;
    try {
      await createTask.mutateAsync({
        departmentId,
        instructions: instructions.trim(),
        title: title.trim() || undefined,
      });
      toast.success(t("Task created", "Aufgabe erstellt"));
      reset();
      onOpenChange(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast.error(
          t(
            "This department has no team lead to receive the task.",
            "Diese Abteilung hat keine Teamleitung, die die Aufgabe annehmen kann.",
          ),
        );
      } else {
        toast.error(
          e instanceof Error
            ? e.message
            : t("Could not create the task", "Aufgabe konnte nicht erstellt werden"),
        );
      }
    }
  }

  const canSubmit = !!departmentId && instructions.trim().length > 0 && !createTask.isPending;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("New task", "Neue Aufgabe")}</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div>
            <label
              htmlFor="task-board-department"
              className="text-xs font-medium text-muted-foreground"
            >
              {t("Department", "Abteilung")}
            </label>
            <select
              id="task-board-department"
              value={departmentId}
              onChange={(e) => setDepartmentId(e.target.value)}
              disabled={departmentsQuery.isPending || departments.length === 0}
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            >
              {departments.length === 0 && (
                <option value="">
                  {departmentsQuery.isPending
                    ? t("Loading…", "Wird geladen…")
                    : t("No departments available", "Keine Abteilungen verfügbar")}
                </option>
              )}
              {departments.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="task-board-title" className="text-xs font-medium text-muted-foreground">
              {t("Title", "Titel")}
              <span className="ml-1 font-normal text-muted-foreground/80">
                {t("(optional)", "(optional)")}
              </span>
            </label>
            <input
              id="task-board-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={t(
                "Short summary — otherwise the instructions are used",
                "Kurze Zusammenfassung — sonst wird die Anweisung verwendet",
              )}
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            />
          </div>

          <div>
            <label
              htmlFor="task-board-instructions"
              className="text-xs font-medium text-muted-foreground"
            >
              {t("Instructions", "Anweisung")}
            </label>
            <textarea
              id="task-board-instructions"
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              rows={4}
              placeholder={t("What should the team lead do?", "Was soll die Teamleitung tun?")}
              className="mt-1 w-full resize-none rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
            />
          </div>
        </div>

        <DialogFooter>
          <button
            type="button"
            onClick={submit}
            disabled={!canSubmit}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal disabled:opacity-50"
          >
            {createTask.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {t("Create task", "Aufgabe erstellen")}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
