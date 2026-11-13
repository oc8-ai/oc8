import { createFileRoute, Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  FileText,
  Paperclip,
  ShieldCheck,
  Trash2,
  X,
  XCircle,
  Zap,
} from "lucide-react";
import { Panel } from "@/components/app-shell";
import { useConfirm } from "@/hooks/use-confirm";
import { departmentById } from "@/lib/mock-data";
import { statusMeta, type Handoff, type HandoffStatus } from "@/lib/collaboration";
import {
  useDeleteHandoffType,
  useHandoffs,
  useHandoffAction,
  useHandoffTypes,
  useStanding,
  type HandoffDTO,
  type HandoffTypeDTO,
} from "@/lib/hooks";
import { useMay } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

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

function buildTimeline(status: HandoffStatus): Handoff["timeline"] {
  const order = ["pending", "accepted", "in_progress", "completed"];
  const reached = status === "rejected" || status === "expired" ? 0 : order.indexOf(status);
  return [
    { label: "created", done: true },
    { label: "accepted", done: reached >= 1 },
    { label: "in progress", done: reached >= 2 },
    { label: "completed", done: reached >= 3 },
  ];
}

function mapHandoff(d: HandoffDTO): Handoff {
  return {
    id: d.id,
    type: d.type || d.handoffTypeId,
    sourceDept: d.sourceDept,
    targetDept: d.targetDept,
    status: d.status as HandoffStatus,
    createdBy: d.createdBy,
    createdAt: d.createdAt,
    age: relativeAge(d.createdAt),
    gate: d.gate === "approval" ? "approval" : "auto",
    payload: d.payload,
    attachments: [],
    timeline: buildTimeline(d.status as HandoffStatus),
    subtasks: [],
  };
}
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";

export const Route = createFileRoute("/handoffs")({
  component: HandoffsPage,
});

type Filter = "all" | "incoming" | "outgoing" | "gated";

function HandoffsPage() {
  const { data: dtos = [] } = useHandoffs();
  const action = useHandoffAction();
  const standing = useStanding();
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<string | null>(null);

  const items = useMemo(() => dtos.map(mapHandoff), [dtos]);

  // "Mine" is the departments the caller actually holds a seat in. It used to be
  // the literal `"entwicklung"` — a mock slug compared against `targetDept`,
  // which the API fills with a department NAME, so Incoming and Outgoing have
  // matched nothing at all since this screen stopped reading mock data.
  const mySeatDepartments = useMemo(
    () => new Set(standing.seats.map((s) => s.departmentId)),
    [standing.seats],
  );

  const filtered = useMemo(() => {
    switch (filter) {
      case "gated":
        return dtos.filter((d) => d.gate === "approval" && d.status === "pending").map(mapHandoff);
      case "incoming":
        return dtos.filter((d) => mySeatDepartments.has(d.targetDepartmentId)).map(mapHandoff);
      case "outgoing":
        return dtos.filter((d) => mySeatDepartments.has(d.sourceDepartmentId)).map(mapHandoff);
      default:
        return items;
    }
  }, [dtos, items, filter, mySeatDepartments]);

  const current = items.find((h) => h.id === selected) ?? null;

  function updateStatus(id: string, status: HandoffStatus, note?: string) {
    const verb =
      status === "accepted"
        ? "accept"
        : status === "rejected"
          ? "reject"
          : status === "completed"
            ? "complete"
            : null;
    if (!verb) return;
    action.mutate(
      { id, action: verb },
      {
        onSuccess: () =>
          toast.success(
            `Handoff ${status.replace("_", " ")}`,
            note ? { description: note } : undefined,
          ),
        onError: (e) => toast.error(String(e)),
      },
    );
  }

  // Incoming/Outgoing mean "into or out of a department of mine", so they are
  // only offered to somebody who holds a seat. An org_admin holds none, and a
  // pill that can only ever return an empty list is worse than no pill.
  const filters: { id: Filter; label: string }[] = [
    { id: "all", label: "All" },
    ...(mySeatDepartments.size > 0
      ? ([
          { id: "incoming", label: "Incoming" },
          { id: "outgoing", label: "Outgoing" },
        ] as const)
      : []),
    { id: "gated", label: "Pending gate" },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {filters.map((f) => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            className={cn(
              "rounded-full border px-3 py-1 text-xs transition",
              filter === f.id
                ? "border-primary/50 bg-primary/10 text-primary"
                : "border-border bg-panel text-muted-foreground hover:text-foreground",
            )}
          >
            {f.label}
          </button>
        ))}
        <span className="ml-auto text-xs text-muted-foreground">
          {filtered.length} handoff{filtered.length === 1 ? "" : "s"}
        </span>
      </div>

      <Panel className="divide-y divide-border">
        {filtered.length === 0 && (
          <div className="p-8 text-center text-sm text-muted-foreground">
            No handoffs match this filter.
          </div>
        )}
        {filtered.map((h) => (
          <HandoffRow key={h.id} h={h} onOpen={() => setSelected(h.id)} />
        ))}
      </Panel>

      <HandoffTypesPanel />

      <Sheet open={!!current} onOpenChange={(v) => !v && setSelected(null)}>
        <SheetContent
          side="right"
          className="w-full sm:max-w-[min(92vw,720px)] overflow-y-auto border-border bg-background p-0"
        >
          {current && (
            <HandoffDrawer
              h={current}
              onApprove={() => {
                updateStatus(current.id, "accepted", "Gate approved");
                setSelected(null);
              }}
              onAccept={() => updateStatus(current.id, "accepted")}
              onReject={(reason) => {
                updateStatus(current.id, "rejected", reason);
                setSelected(null);
              }}
              onComplete={() => updateStatus(current.id, "completed")}
            />
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function HandoffRow({ h, onOpen }: { h: Handoff; onOpen: () => void }) {
  const src = departmentById(h.sourceDept);
  const tgt = departmentById(h.targetDept);
  const meta = statusMeta[h.status];
  return (
    <button
      onClick={onOpen}
      className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-muted/30"
    >
      <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/10 text-primary">
        <FileText className="h-4 w-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate font-mono text-sm">{h.type}</span>
          {h.gate === "approval" && (
            <ShieldCheck className="h-3 w-3 text-[color:var(--status-warning)]" />
          )}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span>{src?.name ?? h.sourceDept}</span>
          <ArrowRight className="h-3 w-3" />
          <span>{tgt?.name ?? h.targetDept}</span>
          <span>·</span>
          <span>by {h.createdBy}</span>
          <span>·</span>
          <span>{h.age}</span>
        </div>
      </div>
      <span
        className="rounded-full border px-2 py-0.5 text-[10px] font-medium capitalize"
        style={{
          color: meta.color,
          borderColor: `color-mix(in oklab, ${meta.color} 35%, transparent)`,
          background: `color-mix(in oklab, ${meta.color} 10%, transparent)`,
        }}
      >
        {meta.label}
      </span>
      <ChevronRight className="h-4 w-4 text-muted-foreground" />
    </button>
  );
}

function HandoffDrawer({
  h,
  onApprove,
  onAccept,
  onReject,
  onComplete,
}: {
  h: Handoff;
  onApprove: () => void;
  onAccept: () => void;
  onReject: (reason: string) => void;
  onComplete: () => void;
}) {
  const src = departmentById(h.sourceDept);
  const tgt = departmentById(h.targetDept);
  const [reason, setReason] = useState("");
  const meta = statusMeta[h.status];
  return (
    <>
      <SheetHeader className="border-b border-border px-6 py-4 text-left">
        <div className="flex items-center gap-2">
          <FileText className="h-4 w-4 text-primary" />
          <SheetTitle className="font-mono text-base">{h.type}</SheetTitle>
          <span
            className="ml-auto rounded-full border px-2 py-0.5 text-[10px] capitalize"
            style={{
              color: meta.color,
              borderColor: `color-mix(in oklab, ${meta.color} 35%, transparent)`,
              background: `color-mix(in oklab, ${meta.color} 10%, transparent)`,
            }}
          >
            {meta.label}
          </span>
        </div>
        <SheetDescription className="text-xs">
          <span className="font-medium">{src?.name ?? h.sourceDept}</span>
          <ArrowRight className="mx-1 inline h-3 w-3" />
          <span className="font-medium">{tgt?.name ?? h.targetDept}</span> · created by{" "}
          {h.createdBy} · {h.createdAt}
        </SheetDescription>
      </SheetHeader>

      <div className="space-y-5 px-6 py-5">
        <section>
          <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
            Status timeline
          </div>
          <ol className="space-y-2">
            {h.timeline.map((t, i) => (
              <li key={i} className="flex items-center gap-2 text-sm">
                <span
                  className={cn(
                    "grid h-5 w-5 place-items-center rounded-full border text-[10px]",
                    t.done
                      ? "border-primary/60 bg-primary/10 text-primary"
                      : "border-border text-muted-foreground",
                  )}
                >
                  {t.done ? "✓" : i + 1}
                </span>
                <span className={cn(t.done ? "text-foreground" : "text-muted-foreground")}>
                  {t.label}
                </span>
                {t.at && (
                  <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                    {t.at}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </section>

        <section>
          <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
            Payload
          </div>
          <div className="rounded-md border border-border bg-panel/60 p-3">
            <dl className="grid grid-cols-2 gap-2 text-xs">
              {Object.entries(h.payload).map(([k, v]) => (
                <div key={k} className="min-w-0">
                  <dt className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    {k}
                  </dt>
                  <dd className="mt-0.5 truncate font-mono">
                    {Array.isArray(v) ? v.join(", ") : String(v)}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        </section>

        {h.attachments.length > 0 && (
          <section>
            <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
              Attachments
            </div>
            <ul className="space-y-1.5">
              {h.attachments.map((a) => (
                <li key={a.name} className="flex items-center gap-2 text-xs">
                  <Paperclip className="h-3 w-3 text-muted-foreground" />
                  <span>{a.name}</span>
                  <span className="ml-auto text-[10px] text-muted-foreground">
                    copied into target scope
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}

        <section>
          <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
            Provenance chain
          </div>
          <div className="space-y-1.5 rounded-md border border-border bg-panel/60 p-3 text-xs">
            <div>
              Source task: <span className="font-mono">{h.type}.request</span> in {src?.name}
            </div>
            <div className="pl-3 text-muted-foreground">↳ handoff {h.id}</div>
            <div className="pl-3">Target tree · {tgt?.name}:</div>
            <ul className="ml-6 space-y-1">
              {h.subtasks.map((s) => (
                <li key={s.id} className="flex items-center gap-2">
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      s.status === "done"
                        ? "bg-[color:var(--status-running)]"
                        : s.status === "in_progress"
                          ? "bg-primary"
                          : "bg-muted-foreground",
                    )}
                  />
                  <span>{s.title}</span>
                  {s.agentId && (
                    <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                      → {s.agentId}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </section>

        {h.status === "pending" && (
          <section className="rounded-md border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/5 p-3">
            <div className="mb-2 flex items-center gap-2 text-xs text-[color:var(--status-warning)]">
              <ShieldCheck className="h-4 w-4" /> Reject reason (optional)
            </div>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={2}
              className="w-full rounded-md border border-border bg-background/40 px-2 py-1 text-xs outline-none focus:border-primary/50"
              placeholder="e.g. budget out of scope"
            />
          </section>
        )}
      </div>

      <footer className="sticky bottom-0 flex flex-wrap items-center justify-end gap-2 border-t border-border bg-panel px-6 py-3">
        {h.status === "pending" && (
          <>
            <button
              onClick={() => onReject(reason || "no reason given")}
              className="inline-flex items-center gap-1 rounded-md border border-[color:var(--status-error)]/40 bg-[color:var(--status-error)]/10 px-3 py-2 text-xs text-[color:var(--status-error)]"
            >
              <XCircle className="h-3.5 w-3.5" /> Reject
            </button>
            <button
              onClick={onApprove}
              className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground"
            >
              <CheckCircle2 className="h-3.5 w-3.5" /> Approve gate
            </button>
          </>
        )}
        {h.status === "accepted" && (
          <button
            onClick={onAccept}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground"
          >
            <Zap className="h-3.5 w-3.5" /> Start work
          </button>
        )}
        {h.status === "in_progress" && (
          <button
            onClick={onComplete}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground"
          >
            <CheckCircle2 className="h-3.5 w-3.5" /> Mark complete
          </button>
        )}
        {(h.status === "completed" || h.status === "rejected") && (
          <Link
            to="/flows"
            className="inline-flex items-center gap-1 rounded-md border border-border bg-panel px-3 py-2 text-xs"
          >
            <X className="h-3.5 w-3.5" /> Close
          </Link>
        )}
      </footer>
    </>
  );
}

/** The tenant's handoff types, each removable while nothing uses it. The
 *  server refuses (409) a type that handoffs were made with or that a binding
 *  or a live flow names, and says which; that reason is shown as is. */
export function HandoffTypesPanel() {
  const t = useT();
  const { data: types = [] } = useHandoffTypes();
  const remove = useDeleteHandoffType();
  const mayManage = useMay()("handoff:manage");
  const { confirm, ConfirmDialog } = useConfirm();

  async function onDelete(ht: HandoffTypeDTO) {
    const ok = await confirm({
      title: t("Delete handoff type?", "Übergabetyp löschen?"),
      description: t(
        `Delete "${ht.name}"? This only works while no handoff, contract binding or flow uses it. This cannot be undone.`,
        `"${ht.name}" löschen? Das geht nur, solange keine Übergabe, keine Vertragsbindung und kein Flow ihn nutzt. Das kann nicht rückgängig gemacht werden.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    remove.mutate(ht.id, {
      onSuccess: () =>
        toast.success(t("Handoff type deleted", "Übergabetyp gelöscht"), {
          description: ht.name,
        }),
      onError: (error) =>
        toast.error(
          t("Could not delete the handoff type", "Übergabetyp konnte nicht gelöscht werden"),
          {
            description: error instanceof Error ? error.message : String(error),
          },
        ),
    });
  }

  if (types.length === 0) return null;
  return (
    <section>
      {ConfirmDialog}
      <h3 className="mb-2 font-serif text-lg">{t("Handoff types", "Übergabetypen")}</h3>
      <Panel className="divide-y divide-border">
        {types.map((ht) => (
          <div key={ht.id} className="flex items-center gap-3 px-4 py-2.5">
            <span className="min-w-0 flex-1 truncate font-mono text-sm">{ht.name}</span>
            <span className="text-[11px] text-muted-foreground">{ht.classification}</span>
            {mayManage && (
              <button
                type="button"
                onClick={() => onDelete(ht)}
                disabled={remove.isPending}
                aria-label={t(`Delete ${ht.name}`, `${ht.name} löschen`)}
                className="rounded-md p-1 text-muted-foreground transition hover:text-destructive disabled:opacity-50"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        ))}
      </Panel>
    </section>
  );
}
