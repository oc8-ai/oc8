// Meine Arbeit — a freely arrangeable widget grid, Android-homescreen-style.
// Approvals/clarifications, reports, budget, and activity are all widgets
// INSIDE the grid now, not a fixed area — see
// docs/superpowers/specs/2026-09-07-my-work-widget-dashboard-design.md.

import { createFileRoute } from "@tanstack/react-router";
import { Check, LayoutTemplate, Pencil, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { newId } from "@/lib/utils";
import { BoardNotices } from "@/components/dashboard/board-notices";
import { DashboardGrid } from "@/components/dashboard/dashboard-grid";
import { SavePresetDialog } from "@/components/dashboard/save-preset-dialog";
import { TemplatePicker } from "@/components/dashboard/template-picker";
import { WidgetPicker } from "@/components/dashboard/widget-picker";
import { WIDGET_REGISTRY } from "@/components/dashboard/widget-registry";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EditionHomeSections } from "@/edition/slots";
import { useConfirm } from "@/hooks/use-confirm";
import {
  useDashboardLayout,
  useSaveDashboardLayout,
  useStanding,
  type DashboardPresetDTO,
  type DashboardTemplateDTO,
  type WidgetInstance,
  type WidgetType,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { useNotifications } from "@/lib/notifications-api";

export const Route = createFileRoute("/workspace")({
  component: WorkspacePage,
});

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

export function WorkspacePage() {
  const t = useT();
  const standing = useStanding();
  const layoutQuery = useDashboardLayout();
  const save = useSaveDashboardLayout();
  // The same query `BoardNotices` reads (one cache entry): a member without a
  // department still reaches a board shared with them directly.
  const boardNotices = useNotifications({ unread: true, kind: "board.available" });
  const hasBoardNotices = (boardNotices.data ?? []).some((n) => n.subjectId);

  const [widgets, setWidgets] = useState<WidgetInstance[] | null>(null);
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const { confirm, ConfirmDialog } = useConfirm();

  // Set the moment `widgets` is seeded from the server response below, and
  // consumed (cleared) the first time the save effect runs afterwards -- so
  // that one debounced save is skipped. Without this, simply opening the
  // page issues a `PUT` that writes the server's own data back to itself
  // (the debounced value starts at `null`, so its `null -> array` seed
  // transition looks exactly like a real edit to the save effect below),
  // and with `compactType="vertical"` this can even silently rewrite stored
  // coordinates to the compacted layout on first paint.
  const skipNextSave = useRef(false);

  // Seed local state from the server once the query resolves. A `null`
  // response (no saved layout yet) is left as `null` here too -- rendering
  // the TemplatePicker below is what a `null` layout means.
  useEffect(() => {
    if (layoutQuery.data !== undefined && widgets === null) {
      if (layoutQuery.data !== null) {
        skipNextSave.current = true;
        setWidgets(layoutQuery.data.widgets);
        setTemplateId(layoutQuery.data.templateId);
      }
    }
    // Only seed once, on the query's first resolution -- subsequent server
    // responses (e.g. from this same save) must never overwrite in-progress
    // local edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutQuery.data]);

  const debouncedWidgets = useDebouncedValue(widgets, 800);
  useEffect(() => {
    if (debouncedWidgets === null) return;
    if (skipNextSave.current) {
      skipNextSave.current = false;
      return;
    }
    save.mutate(
      { widgets: debouncedWidgets, templateId },
      {
        onError: () =>
          toast.error(
            t(
              "Could not save your dashboard layout",
              "Dein Dashboard-Layout konnte nicht gespeichert werden",
            ),
          ),
      },
    );
    // save/templateId/t change identity every render; only a real widgets
    // change should trigger a new debounced PUT.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedWidgets]);

  async function replaceCurrentLayout(): Promise<boolean> {
    // Only asked when a layout already exists -- the very first pick (from
    // the initial `widgets === null` picker) has nothing to overwrite yet.
    if (widgets === null) return true;
    return confirm({
      title: t("Switch layout?", "Layout wechseln?"),
      description: t(
        "This replaces your current arrangement. You can switch back at any time.",
        "Das ersetzt deine aktuelle Anordnung. Du kannst jederzeit wieder wechseln.",
      ),
      confirmLabel: t("Switch", "Wechseln"),
      cancelLabel: t("Cancel", "Abbrechen"),
      destructive: false,
    });
  }

  async function pickTemplate(template: DashboardTemplateDTO) {
    if (!(await replaceCurrentLayout())) return;
    setWidgets(template.widgets);
    setTemplateId(template.id);
    setSwitcherOpen(false);
  }

  async function pickPreset(preset: DashboardPresetDTO) {
    if (!(await replaceCurrentLayout())) return;
    setWidgets(preset.widgets);
    setTemplateId(null);
    setSwitcherOpen(false);
  }

  function addWidget(type: WidgetType, config: Record<string, unknown> = {}) {
    const { defaultSize } = WIDGET_REGISTRY[type];
    setWidgets((prev) => {
      const list = prev ?? [];
      // One tile per dashboard: a repeated add (a double click) is a no-op.
      if (
        type === "data_board" &&
        list.some((w) => w.type === type && w.config?.boardId === config.boardId)
      )
        return list;
      return [
        ...list,
        {
          id: newId(),
          type,
          x: 0,
          // A real bottom-row y, not react-grid-layout's `Infinity`
          // "append below everything" sentinel: `JSON.stringify` serializes
          // `Infinity` as `null`, and the backend's `y: int` field rejects
          // `null`. Normally `onLayoutChange` replaces the sentinel with a
          // real row before the debounced save fires, but if it doesn't
          // (an unmeasured container, a throttled background tab), the add
          // would silently never persist.
          y: list.length ? Math.max(...list.map((w) => w.y + w.h)) : 0,
          w: defaultSize.w,
          h: defaultSize.h,
          config,
        },
      ];
    });
  }

  const unassignedText = t(
    "You are not assigned to a department",
    "Du bist keiner Abteilung zugeordnet",
  );

  if (standing.unassigned && (layoutQuery.isPending || boardNotices.isPending)) {
    return (
      <div className="flex items-center justify-center p-10 text-sm text-muted-foreground">
        {t("Loading…", "Wird geladen…")}
      </div>
    );
  }

  // Without a department there is nothing to arrange -- unless a board was
  // shared with the member directly, or they already keep a layout.
  if (standing.unassigned && widgets === null && !layoutQuery.data && !hasBoardNotices) {
    return (
      <Panel className="p-10 text-center">
        <h2 className="font-serif text-xl">{unassignedText}</h2>
      </Panel>
    );
  }

  // Kept, as a note, for a member without a department who has boards here.
  const unassignedNote = standing.unassigned ? (
    <p className="text-xs text-muted-foreground">{unassignedText}</p>
  ) : null;

  if (layoutQuery.isPending) {
    return (
      <div className="flex items-center justify-center p-10 text-sm text-muted-foreground">
        {t("Loading…", "Wird geladen…")}
      </div>
    );
  }

  // Checked BEFORE the `widgets === null` branch below: a TanStack Query
  // `error` state (retries exhausted) also leaves `widgets` at `null`, and
  // that null looks identical to "first visit, no saved layout yet" -- the
  // one case that renders the TemplatePicker. Falling through to it here
  // would let picking a template full-replace the member's real saved
  // layout with the picked one 800ms later, via the debounced save.
  if (layoutQuery.isError) {
    return (
      <Panel className="p-10 text-center">
        <p className="text-sm text-muted-foreground">
          {t("Your dashboard could not be loaded.", "Dein Dashboard konnte nicht geladen werden.")}
        </p>
        <button
          type="button"
          onClick={() => layoutQuery.refetch()}
          className="mt-3 rounded-md border border-border bg-panel px-3 py-1.5 text-sm transition hover:bg-muted/30"
        >
          {t("Retry", "Erneut versuchen")}
        </button>
      </Panel>
    );
  }

  if (widgets === null) {
    // A board added from a notice becomes the first layout: `addWidget`
    // starts the list, and the debounced save above stores it.
    return (
      <div className="space-y-3">
        {unassignedNote}
        <BoardNotices onAdd={addWidget} boardIdsOnGrid={[]} />
        {/* The layouts are department widgets: no use without a department. */}
        {!standing.unassigned && <TemplatePicker onPick={pickTemplate} onPickPreset={pickPreset} />}
        {ConfirmDialog}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {unassignedNote}
      <EditionHomeSections />
      <div className="flex flex-wrap justify-end gap-2">
        {editing && (
          <>
            <button
              type="button"
              onClick={() => setSaveOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel px-2.5 py-2 text-sm transition hover:bg-muted/30"
            >
              <Save className="h-4 w-4" />
              {t("Save as preset", "Als Preset speichern")}
            </button>
            <button
              type="button"
              onClick={() => setSwitcherOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel px-2.5 py-2 text-sm transition hover:bg-muted/30"
            >
              <LayoutTemplate className="h-4 w-4" />
              {t("Switch layout", "Layout wechseln")}
            </button>
            <WidgetPicker onAdd={addWidget} />
          </>
        )}
        <button
          type="button"
          onClick={() => setEditing((v) => !v)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel px-2.5 py-2 text-sm transition hover:bg-muted/30"
        >
          {editing ? <Check className="h-4 w-4" /> : <Pencil className="h-4 w-4" />}
          {editing ? t("Done", "Fertig") : t("Edit", "Bearbeiten")}
        </button>
      </div>
      <BoardNotices
        onAdd={addWidget}
        boardIdsOnGrid={widgets.flatMap((w) =>
          w.type === "data_board" && typeof w.config?.boardId === "string"
            ? [w.config.boardId]
            : [],
        )}
      />
      <DashboardGrid widgets={widgets} editing={editing} onChange={setWidgets} />
      <Dialog open={switcherOpen} onOpenChange={setSwitcherOpen}>
        <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="sr-only">{t("Switch layout", "Layout wechseln")}</DialogTitle>
          </DialogHeader>
          <TemplatePicker onPick={pickTemplate} onPickPreset={pickPreset} />
        </DialogContent>
      </Dialog>
      <SavePresetDialog open={saveOpen} onOpenChange={setSaveOpen} widgets={widgets} />
      {ConfirmDialog}
    </div>
  );
}
