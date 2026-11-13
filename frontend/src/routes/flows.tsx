import { createFileRoute, Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  AlertTriangle,
  Archive,
  ArrowRight,
  Bolt,
  Building2,
  CheckCircle2,
  ChevronRight,
  Clock,
  Plus,
  ShieldCheck,
  Sparkles,
  Square,
} from "lucide-react";
import { Panel } from "@/components/app-shell";
import { useConfirm } from "@/hooks/use-confirm";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  flowToYaml,
  validateFlowContracts,
  type FlowDef,
  type FlowRun,
  type FlowStage,
} from "@/lib/collaboration";
import {
  useArchiveFlow,
  useCancelFlowRun,
  useDepartments,
  useFlows,
  useFlowRuns,
  usePublishFlow,
  type FlowListDTO,
  type FlowRunDTO,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type SpecStage = {
  id: string;
  handoff?: { type?: string; to_department_id?: string; gate?: string };
};
type SpecShape = { trigger?: { from_department_id?: string }; stages?: SpecStage[] };

function mapFlow(d: FlowListDTO, deptName: (id: string) => string): FlowDef {
  const spec = d.spec as unknown as SpecShape;
  const fromDept = deptName(spec.trigger?.from_department_id ?? "");
  const stages: FlowStage[] = (spec.stages ?? []).map((s) => ({
    id: s.id,
    label: s.id,
    handoffType: s.handoff?.type ?? "",
    sourceDept: fromDept,
    targetDept: deptName(s.handoff?.to_department_id ?? ""),
    gate: s.handoff?.gate === "approval" ? "approval" : "auto",
    timeout: "",
  }));
  const departments = Array.from(new Set([fromDept, ...stages.map((s) => s.targetDept)])).filter(
    Boolean,
  );
  return {
    id: d.id,
    name: d.name,
    version: d.semver,
    status: "active",
    departments,
    runs: d.runCount,
    stages,
    compensations: [],
  };
}

function mapRun(d: FlowRunDTO): FlowRun {
  const stages = (d.currentStages as string[]) ?? [];
  const stageState: Record<string, "pending" | "gated" | "active" | "done"> = {};
  for (const s of stages) stageState[s] = "active";
  return {
    id: d.id,
    flowId: d.flowId,
    currentStageId: stages[0] ?? null,
    customer: String((d.context as Record<string, unknown>)?.customer ?? ""),
    startedAt: "",
    stageState,
    events: [],
  };
}

export const Route = createFileRoute("/flows")({
  component: FlowsPage,
});

export function FlowsPage() {
  const { data: flowDtos = [] } = useFlows();
  const { data: runDtos = [] } = useFlowRuns();
  // Picker over the whole tenant, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<"canvas" | "yaml">("canvas");
  const [openRun, setOpenRun] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const t = useT();
  const { confirm, ConfirmDialog } = useConfirm();
  const archiveFlow = useArchiveFlow();
  const cancelRun = useCancelFlowRun();

  const deptName = useMemo(() => {
    const map = new Map(departments.map((d) => [d.id, d.name]));
    return (id: string) => map.get(id) ?? id;
  }, [departments]);

  const flowList = useMemo(() => flowDtos.map((d) => mapFlow(d, deptName)), [flowDtos, deptName]);
  const allRuns = useMemo(() => runDtos.map(mapRun), [runDtos]);

  const flow = flowList.find((f) => f.id === selected) ?? null;
  const problems = flow ? validateFlowContracts(flow) : [];
  const runs = flow ? allRuns.filter((r) => r.flowId === flow.id) : [];
  const run = openRun ? (allRuns.find((r) => r.id === openRun) ?? null) : null;
  const runStatus = useMemo(() => new Map(runDtos.map((d) => [d.id, d.status])), [runDtos]);

  async function onArchive(f: FlowDef) {
    const ok = await confirm({
      title: t("Archive flow?", "Flow archivieren?"),
      description: t(
        `Archive "${f.name}"? It disappears from this list and can no longer be started. Runs already going keep running; cancel them separately if they should stop. Publishing the flow again brings it back.`,
        `"${f.name}" archivieren? Der Flow verschwindet aus dieser Liste und kann nicht mehr gestartet werden. Laufende Runs laufen weiter; brich sie einzeln ab, wenn sie stoppen sollen. Wenn du den Flow erneut veröffentlichst, ist er wieder da.`,
      ),
      confirmLabel: t("Archive", "Archivieren"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    archiveFlow.mutate(f.id, {
      onSuccess: () => {
        toast.success(t("Flow archived", "Flow archiviert"), { description: f.name });
        setSelected(null);
        setOpenRun(null);
      },
      onError: (error) =>
        toast.error(t("Could not archive the flow", "Flow konnte nicht archiviert werden"), {
          description: error instanceof Error ? error.message : String(error),
        }),
    });
  }

  async function onCancelRun(r: FlowRun) {
    const ok = await confirm({
      title: t("Cancel flow run?", "Flow-Run abbrechen?"),
      description: t(
        "Its open handoffs are rejected and any agent run working on them is stopped. Completed stages stay as they are. This cannot be undone.",
        "Seine offenen Übergaben werden abgelehnt und Agenten-Runs, die daran arbeiten, werden gestoppt. Abgeschlossene Stufen bleiben, wie sie sind. Das kann nicht rückgängig gemacht werden.",
      ),
      confirmLabel: t("Cancel run", "Run abbrechen"),
      cancelLabel: t("Keep running", "Weiterlaufen lassen"),
    });
    if (!ok) return;
    cancelRun.mutate(r.id, {
      onSuccess: () => toast.success(t("Flow run cancelled", "Flow-Run abgebrochen")),
      onError: (error) =>
        toast.error(t("Could not cancel the run", "Run konnte nicht abgebrochen werden"), {
          description: error instanceof Error ? error.message : String(error),
        }),
    });
  }

  return (
    <div className="space-y-5">
      {ConfirmDialog}
      {/* Flow list */}
      <section>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="font-serif text-lg">Flows</h3>
          <button
            onClick={() => setCreateOpen(true)}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
          >
            <Plus className="h-3.5 w-3.5" /> New flow
          </button>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {flowList.map((f) => (
            <button
              key={f.id}
              onClick={() => setSelected(f.id)}
              className={cn(
                "text-left rounded-xl border bg-panel p-4 transition hover:border-primary/50",
                selected === f.id ? "border-primary/60" : "border-border",
              )}
            >
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-primary" />
                <span className="font-serif text-base">{f.name}</span>
                <span className="ml-auto rounded-full border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                  {f.version}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-2 text-[11px]">
                <StatusBadge s={f.status} />
                <span className="text-muted-foreground">· {f.runs} runs</span>
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {f.departments.map((d) => (
                  <DeptChip key={d} id={d} />
                ))}
              </div>
            </button>
          ))}
          {flowList.length === 0 && (
            <Panel className="col-span-full border-dashed px-5 py-10 text-center">
              <Sparkles className="mx-auto h-5 w-5 text-primary" />
              <p className="mt-3 font-medium">No flows yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Create a flow to coordinate a handoff between departments.
              </p>
              <button
                onClick={() => setCreateOpen(true)}
                className="mt-4 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
              >
                Create first flow
              </button>
            </Panel>
          )}
        </div>
      </section>

      <CreateFlowDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        departments={departments}
        onCreated={setSelected}
      />

      {/* Selected flow: canvas / yaml */}
      {flow && (
        <Panel className="overflow-hidden">
          <header className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
            <h4 className="font-serif text-base">{flow.name}</h4>
            <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
              {flow.version}
            </span>
            {problems.length > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full border border-[color:var(--status-error)]/40 bg-[color:var(--status-error)]/10 px-2 py-0.5 text-[10px] text-[color:var(--status-error)]">
                <AlertTriangle className="h-3 w-3" /> {problems.length} contract issue
              </span>
            )}
            <button
              type="button"
              onClick={() => onArchive(flow)}
              disabled={archiveFlow.isPending}
              className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-destructive/50 hover:text-destructive disabled:opacity-50"
            >
              <Archive className="h-3 w-3" /> {t("Archive", "Archivieren")}
            </button>
            <div className="flex overflow-hidden rounded-md border border-border">
              {(["canvas", "yaml"] as const).map((v) => (
                <button
                  key={v}
                  onClick={() => setView(v)}
                  className={cn(
                    "px-3 py-1 text-[11px] uppercase tracking-widest",
                    view === v ? "bg-primary/15 text-primary" : "text-muted-foreground",
                  )}
                >
                  {v}
                </button>
              ))}
            </div>
          </header>

          {view === "canvas" ? (
            <FlowCanvas flow={flow} problems={problems} />
          ) : (
            <pre className="overflow-x-auto bg-background/40 px-4 py-3 text-xs leading-relaxed">
              <code className="font-mono text-foreground/90">{flowToYaml(flow)}</code>
            </pre>
          )}
        </Panel>
      )}

      {/* Runs */}
      {flow && runs.length > 0 && (
        <section>
          <h3 className="mb-2 font-serif text-lg">Runs</h3>
          <Panel className="divide-y divide-border">
            {runs.map((r) => (
              <button
                key={r.id}
                onClick={() => setOpenRun(r.id === openRun ? null : r.id)}
                className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-muted/30"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">{r.customer}</div>
                  <div className="text-[11px] text-muted-foreground">{r.startedAt}</div>
                </div>
                <RunStatusBadge status={runStatus.get(r.id) ?? "running"} />
                <RunPipelineMini flow={flow} run={r} />
                <ChevronRight
                  className={cn(
                    "h-4 w-4 text-muted-foreground transition",
                    openRun === r.id && "rotate-90 text-primary",
                  )}
                />
              </button>
            ))}
          </Panel>

          {run && (
            <RunDetail
              flow={flow}
              run={run}
              status={runStatus.get(run.id) ?? "running"}
              cancelling={cancelRun.isPending}
              onCancel={() => onCancelRun(run)}
            />
          )}
        </section>
      )}
    </div>
  );
}

function flowIdFromName(name: string) {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 60);
}

function CreateFlowDialog({
  open,
  onOpenChange,
  departments,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  departments: { id: string; name: string }[];
  onCreated: (id: string) => void;
}) {
  const publish = usePublishFlow();
  const [name, setName] = useState("");
  const [sourceDepartmentId, setSourceDepartmentId] = useState("");
  const [targetDepartmentId, setTargetDepartmentId] = useState("");
  const [event, setEvent] = useState("work.requested");
  const [handoffType, setHandoffType] = useState("work_item");
  const [gate, setGate] = useState("auto");

  const submit = () => {
    const id = flowIdFromName(name);
    if (!id || !sourceDepartmentId || !targetDepartmentId || !event.trim() || !handoffType.trim()) {
      toast.error("Please complete all flow fields.");
      return;
    }
    if (sourceDepartmentId === targetDepartmentId) {
      toast.error("Choose two different departments.");
      return;
    }
    publish.mutate(
      {
        id,
        version: "1.0.0",
        trigger: { event: event.trim(), from_department_id: sourceDepartmentId },
        stages: [
          {
            id: "handoff",
            handoff: {
              type: handoffType.trim(),
              to_department_id: targetDepartmentId,
              gate,
            },
          },
        ],
      },
      {
        onSuccess: (created) => {
          toast.success("Flow published", { description: created.name });
          onCreated(created.flowId);
          setName("");
          onOpenChange(false);
        },
        onError: (error) =>
          toast.error("Could not publish flow", {
            description: error instanceof Error ? error.message : String(error),
          }),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create flow</DialogTitle>
          <DialogDescription>
            Publish a first handoff stage between two departments. It will appear in this list
            immediately.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 text-sm">
          <label className="grid gap-1.5">
            <span>Flow name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Customer onboarding"
              className="rounded-md border border-input bg-background px-3 py-2"
              autoFocus
            />
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1.5">
              <span>From department</span>
              <select
                value={sourceDepartmentId}
                onChange={(e) => setSourceDepartmentId(e.target.value)}
                className="rounded-md border border-input bg-background px-3 py-2"
              >
                <option value="">Select…</option>
                {departments.map((department) => (
                  <option key={department.id} value={department.id}>
                    {department.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="grid gap-1.5">
              <span>To department</span>
              <select
                value={targetDepartmentId}
                onChange={(e) => setTargetDepartmentId(e.target.value)}
                className="rounded-md border border-input bg-background px-3 py-2"
              >
                <option value="">Select…</option>
                {departments.map((department) => (
                  <option key={department.id} value={department.id}>
                    {department.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1.5">
              <span>Trigger event</span>
              <input
                value={event}
                onChange={(e) => setEvent(e.target.value)}
                className="rounded-md border border-input bg-background px-3 py-2"
              />
            </label>
            <label className="grid gap-1.5">
              <span>Handoff type</span>
              <input
                value={handoffType}
                onChange={(e) => setHandoffType(e.target.value)}
                className="rounded-md border border-input bg-background px-3 py-2"
              />
            </label>
          </div>
          <label className="grid gap-1.5">
            <span>Gate</span>
            <select
              value={gate}
              onChange={(e) => setGate(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2"
            >
              <option value="auto">Automatic</option>
              <option value="approval">Requires approval</option>
            </select>
          </label>
        </div>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={publish.isPending || departments.length < 2}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
          >
            {publish.isPending ? "Publishing…" : "Publish flow"}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function StatusBadge({ s }: { s: FlowDef["status"] }) {
  const map = {
    active: { c: "var(--status-running)", label: "Active" },
    draft: { c: "var(--muted-foreground)", label: "Draft" },
    suspended: { c: "var(--status-error)", label: "Suspended" },
  }[s];
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]"
      style={{
        color: map.c,
        borderColor: `color-mix(in oklab, ${map.c} 35%, transparent)`,
        background: `color-mix(in oklab, ${map.c} 10%, transparent)`,
      }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: map.c }} />
      {map.label}
    </span>
  );
}

// Deterministic accent hue from a department's name, purely cosmetic --
// departments here only ever arrive as already-resolved display-name
// strings (see mapFlow), not ids, so there is no record to look up.
function hashHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return h;
}

function deptAccent(name: string): string {
  return `oklch(0.75 0.15 ${hashHue(name)})`;
}

function DeptChip({ id }: { id: string }) {
  const accent = deptAccent(id);
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px]"
      style={{
        borderColor: `color-mix(in oklab, ${accent} 40%, transparent)`,
        color: accent,
        background: `color-mix(in oklab, ${accent} 10%, transparent)`,
      }}
    >
      <Building2 className="h-2.5 w-2.5" />
      {id}
    </span>
  );
}

function FlowCanvas({
  flow,
  problems,
}: {
  flow: FlowDef;
  problems: { stageId: string; message: string }[];
}) {
  const problemMap = useMemo(
    () => Object.fromEntries(problems.map((p) => [p.stageId, p.message])),
    [problems],
  );
  const rooms = flow.departments;

  return (
    <div className="p-6">
      <div className="mb-6 grid grid-cols-3 gap-4">
        {rooms.map((id) => {
          const accent = deptAccent(id);
          return (
            <div
              key={id}
              className="rounded-xl border border-border bg-background/40 p-3"
              style={{
                borderColor: `color-mix(in oklab, ${accent} 30%, var(--border))`,
              }}
            >
              <div className="flex items-center gap-2">
                <div
                  className="grid h-8 w-8 place-items-center rounded-md"
                  style={{
                    background: `color-mix(in oklab, ${accent} 22%, transparent)`,
                    color: accent,
                  }}
                >
                  <Building2 className="h-4 w-4" />
                </div>
                <div>
                  <div className="font-serif text-sm">{id}</div>
                  <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    Room
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="space-y-3">
        {flow.stages.map((st) => {
          const invalid = problemMap[st.id];
          const srcAccent = deptAccent(st.sourceDept);
          const tgtAccent = deptAccent(st.targetDept);
          return (
            <div
              key={st.id}
              className={cn(
                "flex flex-wrap items-center gap-3 rounded-xl border bg-panel/60 p-3",
                invalid ? "border-[color:var(--status-error)]/50" : "border-border",
              )}
              title={invalid ?? ""}
            >
              <span
                className="rounded-md border border-border bg-background/40 px-2 py-1 text-xs"
                style={{ color: srcAccent }}
              >
                {st.sourceDept}
              </span>
              <ArrowRight
                className={cn(
                  "h-4 w-4",
                  invalid ? "text-[color:var(--status-error)]" : "text-muted-foreground",
                )}
              />
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs">{st.handoffType}</span>
                  {st.gate === "approval" ? (
                    <ShieldCheck className="h-3.5 w-3.5 text-[color:var(--status-warning)]" />
                  ) : (
                    <Bolt className="h-3.5 w-3.5 text-primary" />
                  )}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                  <Clock className="h-2.5 w-2.5" /> {st.timeout}
                </div>
                {invalid && (
                  <div className="mt-1 text-[10px] text-[color:var(--status-error)]">{invalid}</div>
                )}
              </div>
              <span
                className="rounded-md border border-border bg-background/40 px-2 py-1 text-xs"
                style={{ color: tgtAccent }}
              >
                {st.targetDept}
              </span>
            </div>
          );
        })}

        {flow.compensations.map((c) => {
          return (
            <div
              key={c.from}
              className="flex flex-wrap items-center gap-3 rounded-xl border border-dashed p-3 text-xs"
              style={{
                borderColor: "color-mix(in oklab, #FF8A3D 55%, transparent)",
                background: "color-mix(in oklab, #FF8A3D 6%, transparent)",
                color: "#FF8A3D",
              }}
            >
              <AlertTriangle className="h-3.5 w-3.5" />
              <span>Compensation</span>
              <ArrowRight className="h-4 w-4" />
              <span className="font-mono">{c.type}</span>
              <ArrowRight className="h-4 w-4" />
              <span>{c.toDept}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RunPipelineMini({ flow, run }: { flow: FlowDef; run: FlowRun }) {
  return (
    <div className="flex items-center gap-1">
      {flow.stages.map((st, i) => {
        const s = run.stageState[st.id];
        const color =
          s === "done"
            ? "var(--status-running)"
            : s === "active"
              ? "var(--primary)"
              : s === "gated"
                ? "var(--status-warning)"
                : "var(--muted-foreground)";
        return (
          <span key={st.id} className="flex items-center gap-1">
            <span
              className={cn("h-2 w-6 rounded-full", s === "active" && "animate-pulse")}
              style={{ background: color, opacity: s === "pending" ? 0.3 : 1 }}
            />
            {i < flow.stages.length - 1 && (
              <span className="text-[10px] text-muted-foreground">·</span>
            )}
          </span>
        );
      })}
    </div>
  );
}

function RunStatusBadge({ status }: { status: string }) {
  const t = useT();
  const meta: Record<string, { c: string; label: string }> = {
    running: { c: "var(--status-running)", label: t("Running", "Läuft") },
    suspended: { c: "var(--status-warning)", label: t("Suspended", "Pausiert") },
    completed: { c: "var(--muted-foreground)", label: t("Completed", "Abgeschlossen") },
    failed: { c: "var(--status-error)", label: t("Failed", "Fehlgeschlagen") },
    cancelled: { c: "var(--muted-foreground)", label: t("Cancelled", "Abgebrochen") },
  };
  const m = meta[status] ?? { c: "var(--muted-foreground)", label: status };
  return (
    <span
      className="rounded-full border px-1.5 py-0.5 text-[10px]"
      style={{
        color: m.c,
        borderColor: `color-mix(in oklab, ${m.c} 35%, transparent)`,
      }}
    >
      {m.label}
    </span>
  );
}

function RunDetail({
  flow,
  run,
  status,
  cancelling,
  onCancel,
}: {
  flow: FlowDef;
  run: FlowRun;
  status: string;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const t = useT();
  const cancellable = status === "running" || status === "suspended";
  return (
    <Panel className="mt-3 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="font-serif text-base">{run.customer}</div>
          <div className="text-[11px] text-muted-foreground">Run · started {run.startedAt}</div>
        </div>
        {cancellable && (
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelling}
            className="inline-flex items-center gap-1 rounded-md border border-destructive/40 px-2.5 py-1 text-xs text-destructive transition hover:bg-destructive/10 disabled:opacity-50"
          >
            <Square className="h-3 w-3" /> {t("Cancel run", "Run abbrechen")}
          </button>
        )}
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        {flow.stages.map((st, i) => {
          const s = run.stageState[st.id];
          const done = s === "done";
          const active = s === "active";
          const gated = s === "gated";
          const color = done
            ? "var(--status-running)"
            : active
              ? "var(--primary)"
              : gated
                ? "var(--status-warning)"
                : "var(--muted-foreground)";
          return (
            <span key={st.id} className="flex items-center gap-2">
              <span
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs",
                  active && "animate-pulse",
                )}
                style={{
                  color,
                  borderColor: `color-mix(in oklab, ${color} 40%, transparent)`,
                  background: `color-mix(in oklab, ${color} 10%, transparent)`,
                }}
              >
                {done && <CheckCircle2 className="h-3 w-3" />}
                {gated && <ShieldCheck className="h-3 w-3" />}
                {st.label}
              </span>
              {gated && (
                <Link
                  to="/workspace"
                  className="text-[10px] text-primary underline-offset-2 hover:underline"
                >
                  Waiting for approval →
                </Link>
              )}
              {i < flow.stages.length - 1 && (
                <ArrowRight className="h-3 w-3 text-muted-foreground" />
              )}
            </span>
          );
        })}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
            Stages
          </div>
          <ul className="space-y-1.5 text-xs">
            {flow.stages.map((st) => (
              <li key={st.id} className="rounded-md border border-border bg-panel/50 px-2.5 py-1.5">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{st.label}</span>
                  <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                    {st.handoffType}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
            Event log
          </div>
          <ol className="space-y-1.5 text-[11px]">
            {run.events.map((e, i) => (
              <li key={i} className="flex items-center gap-2">
                <span className="font-mono text-muted-foreground">{e.at}</span>
                <span>{e.label}</span>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </Panel>
  );
}
