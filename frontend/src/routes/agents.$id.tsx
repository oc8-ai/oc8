import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  AlertTriangle,
  BookOpen,
  Check,
  CheckCircle2,
  Circle,
  CircleDot,
  Clock,
  Copy,
  Crown,
  Download,
  FileText,
  Lock,
  MessageSquare,
  Play,
  Repeat,
  Save,
  Search,
  Shield,
  ShieldCheck,
  StopCircle,
  Timer,
  Trash2,
  UserCheck,
  Sparkles,
  Plus,
  Webhook,
  X,
  Wrench,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { LineChart, Line, XAxis, CartesianGrid } from "recharts";
import {
  useActivity,
  useAgentKpis,
  useAgentMemory,
  useAgents,
  useAgentSkills,
  useAgentSupervisor,
  useAgentTriggers,
  useAgentWorkspaceFiles,
  type WorkspaceFileDTO,
  useAnswerRun,
  useAssignSkill,
  useCancelRun,
  useCreateAgentTrigger,
  useCreateGrant,
  useDeleteAgent,
  useDeleteAgentMemory,
  useDeleteAgentTrigger,
  useKnowledgeBases,
  useRenameAgent,
  useRun,
  useRunAgent,
  useSetAgentSupervisor,
  useSkills,
  useModels,
  useSwitchAgentModel,
  useTenantKpis,
  useUnassignSkill,
  useUpdateAgentTrigger,
  useMcpConnections,
  useMcpLogins,
  useCreateMcpLogin,
  type McpLoginDTO,
  type ModelDTO,
  type RunDTO,
  type RunTodoDTO,
  type TriggerDTO,
} from "@/lib/hooks";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { CredentialPicker } from "@/components/credential-picker";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";
import { ToolGuardrailTable, describeGuardrailSaveError } from "@/components/tool-guardrail-table";
import { SUBSCRIPTION_PROVIDER, SubscriptionRiskBadge, supportsRawParams } from "@/routes/models";
import {
  extraToPairs,
  pairsToExtra,
  RawParamsEditor,
  type RawParamPair,
} from "@/components/raw-params-editor";
import { CronBuilder } from "@/components/cron-builder";
import { downloadFileAttachment } from "@/lib/api";
import {
  useAgent,
  useUpdateNarrowing,
  type AgentDetail as AgentDetailData,
} from "@/lib/hooks-agent-detail";
import { setViewedAgent } from "@/lib/live/toast-for-event";
import { Panel, StatusPill } from "@/components/app-shell";
import {
  type Agent,
  type AgentStatus,
  type ActivityItem,
  type KnowledgeBase,
  type Supervisor,
} from "@/lib/mock-data";
import { type Skill } from "@/lib/skills";
import { AgentRuntimePanel } from "@/components/agent-runtime-panel";
import { ChatWindow } from "@/components/chat-window";
import { ComponentGrantPanel } from "@/components/component-grant-panel";
import { InlineRename } from "@/components/inline-rename";
import { AgentInstructionsPanel } from "@/components/agent-instructions-panel";
import { KnowledgeAssignment } from "@/components/knowledge-assignment";
import { MemoryPanel } from "@/components/memory-panel";
import { RUN_COMPONENT_REGISTRY } from "@/components/run-record-card";
import { StatCard } from "@/components/stat-card";
import { formatMs } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import { useMayManageAgent } from "@/lib/governance-hooks";
import { useConfirm } from "@/hooks/use-confirm";

// Mirrors mapAgentStatus in src/lib/live/apply-event.ts (not exported there).
// Handles BOTH vocabularies: the WS "agent.status" event carries the raw
// backend column ("idle", "waiting_for_approval", ...), while the REST
// agent endpoints already pre-map through AGENT_STATUS_TO_UI
// (backend/src/oc8/api/v1/_serializers.py) and send "waiting_for_task"
// directly -- so an already-mapped value must pass through unchanged
// rather than fall into the "anything else" bucket below.
function mapAgentStatus(s: string): AgentStatus {
  if (s === "running") return "running";
  if (s === "error") return "error";
  if (s === "idle" || s === "waiting_for_task") return "waiting_for_task";
  if (s === "paused" || s === "waiting_for_approval" || s === "pending_approval") return "paused";
  // stopped | anything else -> paused
  return "paused";
}

export const Route = createFileRoute("/agents/$id")({
  component: AgentDetail,
  notFoundComponent: () => (
    <div className="p-8 text-center text-muted-foreground">Agent not found.</div>
  ),
});

const TAB_IDS = [
  "overview",
  "instructions",
  "guardrails",
  "livelog",
  "chat",
  "files",
  "config",
  "skills",
  "memory",
  "history",
] as const;
type TabId = (typeof TAB_IDS)[number];

function AgentDetail() {
  const t = useT();
  const { id } = Route.useParams();
  const { data: agent, isLoading } = useAgent(id);
  // Suppress toasts (Toast v2) for events about this agent while its Live Log
  // is open — the user is already watching it live.
  useEffect(() => {
    setViewedAgent(id);
    return () => setViewedAgent(null);
  }, [id]);
  // KB-assignment picker for this agent, not a paginated list view.
  const { data: allBasesPage } = useKnowledgeBases({ pageSize: 200 });
  const allBases = allBasesPage?.items ?? [];
  const runAgent = useRunAgent();
  const { data: models = [] } = useModels();
  // WRITE authority for THIS agent — tenant-wide `agent:manage`, OR a live seat
  // in the agent's OWN department carrying the `agentManage` toggle. Neither
  // is the `agent:view` that put the page on the screen. Gates all three
  // agent:manage-reachable controls below: the narrowing editor's Save, the
  // skill-assign button, and the model select.
  const mayManage = useMayManageAgent()(agent?.departmentId);
  const renameAgent = useRenameAgent();
  const [tab, setTab] = useState<TabId>("overview");
  // Id of the most recently enqueued run for this agent (drives the Live Log
  // transcript below via useRun). Resets on page load — the user re-runs to
  // reattach to a fresh run; older runs remain visible in History.
  const [runId, setRunId] = useState<string | null>(null);
  const [runPickerOpen, setRunPickerOpen] = useState(false);
  const [runTask, setRunTask] = useState("");
  // Which knowledge bases are enabled directly on this agent. Seeded from the
  // real bases (useKnowledgeBases) once both the agent and the bases resolve.
  const [agentKbs, setAgentKbs] = useState<string[]>([]);
  useEffect(() => {
    if (!agent) return;
    setAgentKbs(allBases.filter((kb) => kb.linkedAgents.includes(agent.id)).map((kb) => kb.id));
  }, [agent, allBases]);

  if (isLoading) {
    return (
      <div className="space-y-6">
        <Link
          to="/agents"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-primary"
        >
          <ArrowLeft className="h-4 w-4" /> {t("Back to agents", "Zurück zu Agenten")}
        </Link>
        <Panel className="p-8 text-center text-sm text-muted-foreground">
          {t("Loading agent…", "Agent wird geladen…")}
        </Panel>
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="space-y-6">
        <Link
          to="/agents"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-primary"
        >
          <ArrowLeft className="h-4 w-4" /> {t("Back to agents", "Zurück zu Agenten")}
        </Link>
        <Panel className="p-8 text-center text-sm text-muted-foreground">
          {t(
            "Agent not found, or you don't have access.",
            "Agent nicht gefunden oder kein Zugriff.",
          )}
        </Panel>
      </div>
    );
  }

  const status = mapAgentStatus(agent.status);
  const deptEnabled = agent.departmentId
    ? allBases.filter((kb) => kb.linkedDepartments.includes(agent.departmentId!)).map((kb) => kb.id)
    : [];

  const tabs: { id: TabId; label: string }[] = [
    { id: "overview", label: t("Overview", "Übersicht") },
    { id: "instructions", label: t("Instructions", "Anweisungen") },
    { id: "guardrails", label: t("Tools & Access", "Tools & Zugriff") },
    { id: "livelog", label: t("Live Log", "Live-Log") },
    { id: "chat", label: t("Chat", "Chat") },
    { id: "files", label: t("Files", "Dateien") },
    { id: "config", label: t("Configuration", "Konfiguration") },
    { id: "skills", label: t("Skills", "Skills") },
    { id: "memory", label: t("Memory", "Gedächtnis") },
    { id: "history", label: t("History", "Verlauf") },
  ];

  function submitRun() {
    // Task is optional -- the agent's own Instructions (mission) are already
    // sent as its standing system prompt on every run (agent/preamble.py),
    // so a blank field isn't a blank run. This mirrors the placeholder
    // ScheduleEditor/new-agent-dialog.tsx already send for cron-triggered
    // runs, which have no human typing a task either.
    const task =
      runTask.trim() || t(`Manual run for ${agent!.name}`, `Manueller Lauf für ${agent!.name}`);
    runAgent.mutate(
      { agentId: agent!.id, task },
      {
        onSuccess: (data: RunDTO) => {
          setRunId(data.id);
          setRunPickerOpen(false);
          setRunTask("");
          setTab("livelog");
          toast.success(t(`Run started · ${agent!.name}`, `Lauf gestartet · ${agent!.name}`));
        },
        onError: () => toast.error(t("Couldn't start run", "Lauf konnte nicht gestartet werden")),
      },
    );
  }

  return (
    <div className="space-y-6">
      <Link
        to="/agents"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-primary"
      >
        <ArrowLeft className="h-4 w-4" /> {t("Back to agents", "Zurück zu Agenten")}
      </Link>

      <Panel className="p-6">
        <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-4 sm:flex sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-4">
            <div className="relative shrink-0">
              <div
                className="grid h-14 w-14 place-items-center rounded-xl font-serif text-2xl text-black"
                style={{ background: agent.avatarColor }}
              >
                {agent.name[0]}
              </div>
              {agent.isLead && (
                <Crown
                  className="absolute -top-2 -right-2 h-5 w-5 text-[color:var(--status-warning)]"
                  fill="currentColor"
                />
              )}
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <InlineRename
                  value={agent.name}
                  disabled={!mayManage}
                  label={t("Rename agent", "Agent umbenennen")}
                  headingClassName="truncate font-serif text-3xl"
                  onSave={(name) =>
                    renameAgent.mutateAsync(
                      { agentId: agent.id, name },
                      {
                        onError: () =>
                          toast.error(
                            t("Couldn't rename the agent", "Agent konnte nicht umbenannt werden"),
                          ),
                      },
                    )
                  }
                />
                {agent.isLead && (
                  <span className="inline-flex items-center gap-1 rounded-full border border-[color:var(--status-warning)]/50 bg-[color:var(--status-warning)]/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-[color:var(--status-warning)]">
                    <Crown className="h-3 w-3" fill="currentColor" />{" "}
                    {t("Team lead", "Teamleitung")}
                  </span>
                )}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                <span>{agent.role}</span>
                <span>·</span>
                <span>{agent.llm}</span>
                {agent.departmentName && (
                  <>
                    <span>·</span>
                    <span>{agent.departmentName}</span>
                  </>
                )}
              </div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <StatusPill status={status} />
            <DeleteAgentButton agent={agent} mayManage={mayManage} />
            <button
              type="button"
              onClick={() => setRunPickerOpen(true)}
              disabled={status === "running" || runAgent.isPending}
              title={status === "running" ? t("Already running", "Läuft bereits") : undefined}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Play className="h-4 w-4" /> {t("Run now", "Jetzt ausführen")}
            </button>
          </div>
        </div>
      </Panel>

      {runPickerOpen && (
        <div
          className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setRunPickerOpen(false)}
        >
          <div
            className="w-full max-w-md overflow-hidden rounded-xl border border-border bg-panel shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <div>
                <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
                  {t("New run", "Neuer Lauf")}
                </div>
                <h2 className="font-serif text-xl">{t("Run now", "Jetzt ausführen")}</h2>
              </div>
              <button
                type="button"
                onClick={() => setRunPickerOpen(false)}
                className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="p-5">
              <label className="block text-xs uppercase tracking-wider text-muted-foreground">
                {t("Task (optional)", "Aufgabe (optional)")}
              </label>
              <textarea
                autoFocus
                rows={4}
                value={runTask}
                onChange={(e) => setRunTask(e.target.value)}
                placeholder={t(
                  `Optional — ${agent.name} already has standing Instructions. Leave blank to just run those.`,
                  `Optional — ${agent.name} hat bereits Anweisungen hinterlegt. Leer lassen, um einfach damit zu starten.`,
                )}
                className="mt-2 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
              />
              <div className="mt-4 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setRunPickerOpen(false)}
                  className="rounded-md border border-border px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
                >
                  {t("Cancel", "Abbrechen")}
                </button>
                <button
                  type="button"
                  onClick={submitRun}
                  disabled={runAgent.isPending}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
                >
                  <Play className="h-4 w-4" />
                  {runAgent.isPending ? t("Running…", "Läuft…") : t("Run", "Ausführen")}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="flex gap-1 overflow-x-auto border-b border-border">
        {tabs.map((tb) => (
          <button
            key={tb.id}
            onClick={() => setTab(tb.id)}
            className={cn(
              "-mb-px border-b-2 px-4 py-2 text-sm transition",
              tab === tb.id
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {tb.label}
          </button>
        ))}
      </div>

      {tab === "overview" && <OverviewTab agent={agent} />}

      {/* The locally started run wins -- it is the one this operator just
          asked for -- but a run started by a schedule or another operator is
          followed too, which is what makes a cron-driven agent watchable at
          all. */}
      {tab === "livelog" && (
        <LiveLog agentId={agent.id} runId={runId ?? agent.currentRunId ?? null} />
      )}

      {tab === "chat" && <ChatWindow key={agent.id} agentId={agent.id} agentName={agent.name} />}

      {tab === "files" && <WorkspaceFilesPanel agentId={agent.id} />}

      {tab === "config" && (
        <div className="grid gap-4 md:grid-cols-2">
          <div className="md:col-span-2">
            <SupervisorPanel agent={agent} />
          </div>
          <Panel className="p-5">
            <ConfigSectionHeader
              hint={t("agent identity", "Agenten-Identität")}
              title={t("Role", "Rolle")}
            />
            <input
              defaultValue={agent.role}
              className="mt-4 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
          </Panel>
          <AssignedModelPanel agent={agent} models={models} mayManage={mayManage} />
          <AgentRuntimePanel
            agentId={agent.id}
            runtimeRef={agent.runtimeRef}
            mayManage={mayManage}
          />
          <Panel className="p-5">
            <ConfigSectionHeader
              hint={t("when this agent runs", "wann dieser Agent läuft")}
              title={t("Schedule / trigger", "Zeitplan / Auslöser")}
            />
            <TriggerEditor initial={agent.schedule} agentName={agent.name} agentId={agent.id} />
          </Panel>
          <div className="md:col-span-2">
            <AgentKnowledgeSection
              agentId={agent.id}
              agentName={agent.name}
              mayManage={mayManage}
              allBases={allBases}
              agentKbs={agentKbs}
              deptEnabled={deptEnabled}
            />
          </div>
          <ComponentGrantPanel
            granteeType="agent"
            granteeId={agent.id}
            mayManage={mayManage}
            departmentId={agent.departmentId}
          />
        </div>
      )}

      {tab === "instructions" && (
        <AgentInstructionsPanel
          agentId={agent.id}
          agentName={agent.name}
          mission={agent.mission}
          mayManage={mayManage}
        />
      )}

      {tab === "guardrails" && agent.departmentId && (
        <AgentGuardrailsPanel agent={agent} mayManage={mayManage} />
      )}
      {tab === "guardrails" && !agent.departmentId && (
        <Panel className="p-5 text-sm text-muted-foreground">
          {t(
            `${agent.name} is not assigned to a department – tools are defined at the department level.`,
            `${agent.name} ist keiner Abteilung zugeordnet – Tools werden auf Abteilungsebene definiert.`,
          )}
        </Panel>
      )}

      {tab === "memory" && <AgentMemoryTab agentId={agent.id} mayManage={mayManage} />}

      {tab === "history" && (
        <Panel className="p-10 text-center">
          <Clock className="mx-auto mb-3 h-6 w-6 text-muted-foreground/60" />
          <p className="text-sm text-muted-foreground">
            {t(
              "Run history browsing is not yet available.",
              "Das Durchsuchen des Ausführungsverlaufs ist noch nicht verfügbar.",
            )}
          </p>
        </Panel>
      )}

      {tab === "skills" && (
        <AgentSkillsTab agentId={agent.id} agentName={agent.name} mayManage={mayManage} />
      )}
    </div>
  );
}

// Own component (rather than inline in AgentDetail) for the same reason as
// OverviewTab/AgentSkillsTab below: reachable from a test without standing
// up the whole routed page.
export function DeleteAgentButton({
  agent,
  mayManage,
}: {
  agent: AgentDetailData;
  mayManage: boolean;
}) {
  const t = useT();
  const navigate = useNavigate();
  const { confirm, ConfirmDialog } = useConfirm();
  const deleteAgent = useDeleteAgent();

  if (!mayManage) return null;

  async function handleDelete() {
    const ok = await confirm({
      title: t("Delete this agent?", "Diesen Agenten löschen?"),
      description: t(
        `Delete ${agent.name}? Agents with past runs are archived instead and can be restored later; agents with no runs are removed permanently.`,
        `${agent.name} löschen? Agenten mit bisherigen Läufen werden stattdessen archiviert und können später wiederhergestellt werden; Agenten ohne Läufe werden endgültig entfernt.`,
      ),
      confirmLabel: t("Delete", "Löschen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    deleteAgent.mutate(agent.id, {
      onSuccess: (data) => {
        toast.success(
          data.outcome === "archived"
            ? t("Agent archived", "Agent archiviert")
            : t("Agent deleted", "Agent gelöscht"),
          { description: agent.name },
        );
        navigate({ to: "/agents" });
      },
      onError: () =>
        toast.error(t("Couldn't delete the agent", "Agent konnte nicht gelöscht werden")),
    });
  }

  return (
    <>
      <button
        type="button"
        onClick={handleDelete}
        disabled={deleteAgent.isPending}
        title={t("Delete agent", "Agent löschen")}
        className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-2 text-sm text-muted-foreground transition hover:border-[color:var(--status-error)]/50 hover:text-[color:var(--status-error)] disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Trash2 className="h-4 w-4" />
      </button>
      {ConfirmDialog}
    </>
  );
}

// Overview tab: real KPI cards (Task 6's useAgentKpis) plus a 30-day run-count
// trend line. Its own component (rather than inline in AgentDetail) so it's
// reachable from a test without standing up the whole routed page -- same
// shape as AssignedModelPanel/AgentGuardrailsPanel/SupervisorPanel below.
//
// The trend graph calls useTenantKpis({ agentId, groupBy: "day" }) rather than
// extending useAgentKpis with a groupBy param: Task 6's agent-scoped hook
// signature (dateFrom/dateTo only) is already approved and consumed elsewhere,
// and GET /kpis already supports groupBy for exactly this shape (Task 4) --
// reusing it here needs no backend or Task-6-interface change.
export function OverviewTab({ agent }: { agent: AgentDetailData }) {
  const t = useT();
  const kpis = useAgentKpis(agent.id);
  const trend = useTenantKpis({ agentId: agent.id, groupBy: "day" });
  const trendData =
    trend.data && "rows" in trend.data
      ? trend.data.rows.map((r) => ({ date: r.groupKey, runs: r.runCount }))
      : [];

  return (
    <div className="grid gap-4 md:grid-cols-3">
      <StatCard
        icon={<Repeat className="h-4 w-4" />}
        label={t("Runs", "Runs")}
        value={kpis.data ? String(kpis.data.runCount) : "—"}
      />
      <StatCard
        icon={<Clock className="h-4 w-4" />}
        label={t("Avg. duration", "Ø Dauer")}
        value={formatMs(kpis.data?.totalDurationMs)}
      />
      <StatCard
        icon={<Timer className="h-4 w-4" />}
        label={t("Approval wait", "Approval-Wartezeit")}
        value={formatMs(kpis.data?.approvalWaitMs)}
      />
      <Panel className="p-5 md:col-span-3">
        <div className="mb-2 text-xs uppercase tracking-wider text-muted-foreground">
          {t("Runs, last 30 days", "Runs, letzte 30 Tage")}
        </div>
        <ChartContainer
          className="aspect-auto h-48"
          config={{ runs: { label: t("Runs", "Runs"), color: "var(--primary)" } }}
        >
          <LineChart data={trendData}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="date" tickLine={false} axisLine={false} />
            <ChartTooltip content={<ChartTooltipContent />} />
            <Line dataKey="runs" stroke="var(--color-runs)" strokeWidth={2} dot={false} />
          </LineChart>
        </ChartContainer>
      </Panel>
      <Panel className="p-5 md:col-span-3">
        <div className="mb-2 text-xs uppercase tracking-wider text-muted-foreground">
          {t("Last action", "Letzte Aktion")}
        </div>
        <p className="text-sm">{agent.lastAction}</p>
        <div className="mt-3 text-xs text-muted-foreground">{agent.lastRun}</div>
      </Panel>
    </div>
  );
}

// SkillDTO.currentVersionId (backend dto.py) is delivered over the wire but not
// yet declared on the shared Skill type — read it defensively. Null means the
// skill has no published version, so it cannot be assigned.
function skillVersionId(s: Skill): string | null {
  return (s as { currentVersionId?: string | null }).currentVersionId ?? null;
}

// Wraps the existing read-only <KnowledgeAssignment> summary with an
// "Assign knowledge base" button + search modal (mirrors AgentSkillsTab's
// "Assign skill" pattern) -- lets an operator attach a KB directly to this
// agent independent of its department, via the same POST /knowledge/grants
// (useCreateGrant, granteeType="agent") the summary below was already
// wired for but had no entry point to reach (readOnly hid it entirely).
function AgentKnowledgeSection({
  agentId,
  agentName,
  mayManage,
  allBases,
  agentKbs,
  deptEnabled,
}: {
  agentId: string;
  agentName: string;
  mayManage: boolean;
  allBases: KnowledgeBase[];
  agentKbs: string[];
  deptEnabled: string[];
}) {
  const t = useT();
  const createGrant = useCreateGrant();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [search, setSearch] = useState("");

  // Not yet reachable through this agent at all -- already-inherited (dept)
  // or already-granted (agent) bases stay in the read-only summary below,
  // never offered again here.
  const assignable = allBases.filter(
    (kb) => !agentKbs.includes(kb.id) && !deptEnabled.includes(kb.id),
  );
  const searchTerm = search.trim().toLowerCase();
  const filteredAssignable = searchTerm
    ? assignable.filter(
        (kb) =>
          kb.name.toLowerCase().includes(searchTerm) ||
          kb.description.toLowerCase().includes(searchTerm),
      )
    : assignable;

  function assign(kb: KnowledgeBase) {
    createGrant.mutate(
      { kbId: kb.id, granteeType: "agent", granteeId: agentId },
      {
        onSuccess: () => {
          setPickerOpen(false);
          toast.success(t("Knowledge base assigned", "Wissensbasis zugewiesen"), {
            description: `${kb.name} → ${agentName}`,
          });
        },
        onError: () =>
          toast.error(
            t("Couldn't assign knowledge base", "Wissensbasis konnte nicht zugewiesen werden"),
            { description: kb.name },
          ),
      },
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => {
            setSearch("");
            setPickerOpen(true);
          }}
          disabled={!mayManage}
          className="inline-flex shrink-0 items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Plus className="h-4 w-4" /> {t("Assign knowledge base", "Wissensbasis zuweisen")}
        </button>
      </div>
      <KnowledgeAssignment
        mode="agent"
        bases={allBases}
        agentId={agentId}
        agentName={agentName}
        enabled={agentKbs}
        departmentEnabled={deptEnabled}
        readOnly
      />

      {pickerOpen && (
        <div
          className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setPickerOpen(false)}
        >
          <div
            className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-panel shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <div>
                <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
                  {t("Knowledge library", "Wissens-Bibliothek")}
                </div>
                <h2 className="font-serif text-xl">
                  {t("Assign knowledge base", "Wissensbasis zuweisen")}
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setPickerOpen(false)}
                className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="relative border-b border-border px-5 py-3">
              <Search className="pointer-events-none absolute left-8 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("Search knowledge bases…", "Wissensbasen durchsuchen…")}
                className="w-full rounded-md border border-border bg-background/40 py-2 pl-8 pr-3 text-sm outline-none focus:border-primary/50"
              />
            </div>
            <div className="max-h-[60vh] divide-y divide-border overflow-y-auto">
              {filteredAssignable.length === 0 && (
                <div className="p-6 text-center text-sm text-muted-foreground">
                  {assignable.length === 0
                    ? t(
                        "All knowledge bases are already linked.",
                        "Alle Wissensbasen sind bereits verknüpft.",
                      )
                    : t(
                        "No knowledge bases match your search.",
                        "Keine Wissensbasen passen zur Suche.",
                      )}
                </div>
              )}
              {filteredAssignable.map((kb) => (
                <button
                  key={kb.id}
                  type="button"
                  onClick={() => assign(kb)}
                  disabled={createGrant.isPending}
                  className="flex w-full items-start gap-3 px-5 py-3 text-left transition hover:bg-primary/5 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <BookOpen className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <div className="min-w-0 flex-1">
                    <span className="truncate text-sm font-medium">{kb.name}</span>
                    <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                      {kb.docs.toLocaleString()} docs · {kb.description}
                    </p>
                  </div>
                  <Plus className="mt-1 h-4 w-4 text-muted-foreground" />
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function AgentMemoryTab({ agentId, mayManage }: { agentId: string; mayManage: boolean }) {
  const t = useT();
  const memory = useAgentMemory(agentId);
  const deleteMemory = useDeleteAgentMemory(agentId);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  function handleDelete(recordId: string) {
    setDeletingId(recordId);
    deleteMemory.mutate(recordId, {
      onSuccess: () => toast.success(t("Memory deleted", "Erinnerung gelöscht")),
      onError: () =>
        toast.error(t("Couldn't delete memory", "Erinnerung konnte nicht gelöscht werden")),
      onSettled: () => setDeletingId(null),
    });
  }

  return (
    <MemoryPanel
      records={memory.data?.records}
      isLoading={memory.isLoading}
      mayManage={mayManage}
      onDelete={handleDelete}
      deletingId={deletingId}
      emptyLabel={t(
        "This agent hasn't written any memories yet.",
        "Dieser Agent hat noch keine Erinnerungen gespeichert.",
      )}
    />
  );
}

export function AgentSkillsTab({
  agentId,
  agentName,
  mayManage,
}: {
  agentId: string;
  agentName: string;
  mayManage: boolean;
}) {
  const t = useT();
  // Skill-assignment picker for this agent, not a paginated list view.
  const { data: skillsPage } = useSkills({ pageSize: 200 });
  const skills = skillsPage?.items ?? [];
  const { data: agentSkills } = useAgentSkills(agentId);
  const assignSkill = useAssignSkill();
  const unassignSkill = useUnassignSkill();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [search, setSearch] = useState("");

  const assigned = agentSkills ?? [];
  const assignedSkillIds = new Set(assigned.map((a) => a.skillId));
  const assignedSkills = skills.filter((s) => assignedSkillIds.has(s.id));
  const available = skills.filter((s) => !assignedSkillIds.has(s.id));
  const searchTerm = search.trim().toLowerCase();
  const filteredAvailable = searchTerm
    ? available.filter(
        (s) =>
          s.name.toLowerCase().includes(searchTerm) ||
          s.description.toLowerCase().includes(searchTerm),
      )
    : available;

  const assign = (s: Skill) => {
    const versionId = skillVersionId(s);
    if (!versionId) return; // no publishable version → assign is disabled
    setPickerOpen(false);
    // Backend expects a SkillVersion UUID; SkillDTO.currentVersionId carries the
    // current published version (null when the skill has none yet).
    assignSkill.mutate(
      { agentId, skillVersionId: versionId },
      {
        onSuccess: () => {
          toast.success(t("Skill assigned", "Skill zugewiesen"), {
            description: `${s.name} → ${agentName}`,
          });
        },
        onError: () =>
          toast.error(t("Couldn't assign skill", "Skill konnte nicht zugewiesen werden"), {
            description: s.name,
          }),
      },
    );
  };
  const remove = (s: Skill) => {
    const assignment = assigned.find((a) => a.skillId === s.id);
    if (!assignment) return;
    unassignSkill.mutate(
      { agentId, assignmentId: assignment.id },
      {
        onSuccess: () => toast(t("Skill removed", "Skill entfernt"), { description: s.name }),
        onError: () =>
          toast.error(t("Couldn't remove skill", "Skill konnte nicht entfernt werden"), {
            description: s.name,
          }),
      },
    );
  };

  return (
    <div className="space-y-4">
      <Panel className="p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="font-serif text-lg">{t("Assigned skills", "Zugewiesene Skills")}</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              {t(
                `${agentName} is composed of a role plus the skills below. Each skill brings its own tools, knowledge and guardrails.`,
                `${agentName} besteht aus einer Rolle plus den folgenden Skills. Jeder Skill bringt eigene Tools, Wissen und Guardrails mit.`,
              )}
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              setSearch("");
              setPickerOpen(true);
            }}
            disabled={!mayManage}
            className="inline-flex shrink-0 items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Plus className="h-4 w-4" /> {t("Assign skill", "Skill zuweisen")}
          </button>
        </div>
      </Panel>

      {assignedSkills.length === 0 && (
        <Panel className="p-8 text-center text-sm text-muted-foreground">
          {t(
            "No skills assigned yet. Click ‘Assign skill’ to compose this agent.",
            "Noch keine Skills zugewiesen. Klicke ‚Skill zuweisen‘, um diesen Agenten zusammenzustellen.",
          )}
        </Panel>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        {assignedSkills.map((s) => (
          <Panel key={s.id} className="p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-primary" />
                  <h4 className="truncate font-medium">{s.name}</h4>
                  <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                    v{s.version}
                  </span>
                </div>
                <p className="mt-1 text-xs text-muted-foreground line-clamp-2">{s.description}</p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {s.tools.slice(0, 4).map((tool) => (
                    <span
                      key={tool}
                      className="inline-flex items-center gap-1 rounded border border-border bg-background/50 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                    >
                      <Wrench className="h-2.5 w-2.5" /> {tool}
                    </span>
                  ))}
                </div>
              </div>
              <button
                type="button"
                onClick={() => remove(s)}
                aria-label={t("Remove", "Entfernen")}
                className="grid h-7 w-7 shrink-0 place-items-center rounded-md border border-border text-muted-foreground transition hover:border-[color:var(--status-error)] hover:text-[color:var(--status-error)]"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          </Panel>
        ))}
      </div>

      {pickerOpen && (
        <div
          className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setPickerOpen(false)}
        >
          <div
            className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-panel shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <div>
                <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
                  {t("Skill library", "Skill-Bibliothek")}
                </div>
                <h2 className="font-serif text-xl">{t("Assign skill", "Skill zuweisen")}</h2>
              </div>
              <button
                type="button"
                onClick={() => setPickerOpen(false)}
                className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="relative border-b border-border px-5 py-3">
              <Search className="pointer-events-none absolute left-8 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("Search skills…", "Skills durchsuchen…")}
                className="w-full rounded-md border border-border bg-background/40 py-2 pl-8 pr-3 text-sm outline-none focus:border-primary/50"
              />
            </div>
            <div className="max-h-[60vh] divide-y divide-border overflow-y-auto">
              {filteredAvailable.length === 0 && (
                <div className="p-6 text-center text-sm text-muted-foreground">
                  {available.length === 0
                    ? t("All skills already assigned.", "Alle Skills bereits zugewiesen.")
                    : t("No skills match your search.", "Keine Skills passen zur Suche.")}
                </div>
              )}
              {filteredAvailable.map((s) => {
                const noVersion = !skillVersionId(s);
                return (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => assign(s)}
                    disabled={noVersion}
                    className={cn(
                      "flex w-full items-start gap-3 px-5 py-3 text-left transition",
                      noVersion ? "cursor-not-allowed opacity-50" : "hover:bg-primary/5",
                    )}
                  >
                    <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">{s.name}</span>
                        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                          v{s.version}
                        </span>
                        {noVersion && (
                          <span className="rounded-full border border-border px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-muted-foreground">
                            {t("no version", "keine Version")}
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                        {s.description}
                      </p>
                    </div>
                    <Plus className="mt-1 h-4 w-4 text-muted-foreground" />
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!on)}
      className={cn(
        "relative h-5 w-9 rounded-full border transition",
        on ? "border-primary bg-primary/40" : "border-border bg-background/40",
      )}
      aria-pressed={on}
    >
      <span
        className={cn(
          "absolute top-0.5 h-3.5 w-3.5 rounded-full transition-all",
          on
            ? "left-4 bg-primary shadow-[0_0_10px_var(--primary)]"
            : "left-0.5 bg-muted-foreground",
        )}
      />
    </button>
  );
}

// ---------- Guardrails / tool narrowing ----------

// The agent's Assigned-LLM panel. Its own component (rather than inline in
// AgentDetail) so the subscription risk badge below is reachable from a test
// without standing up the whole routed page -- same shape as AgentGuardrailsPanel
// and AgentRuntimePanel next door.
export function AssignedModelPanel({
  agent,
  models,
  mayManage,
}: {
  agent: AgentDetailData;
  models: ModelDTO[];
  mayManage: boolean;
}) {
  const t = useT();
  const switchModel = useSwitchAgentModel();
  const assigned = models.find((model) => model.id === agent.modelConfigId);
  const showEffort = assigned?.provider === "anthropic";
  const showRawParams = !!assigned && supportsRawParams(assigned.provider);

  const [temperature, setTemperature] = useState(
    agent.temperature != null ? String(agent.temperature) : "",
  );
  const [maxTokens, setMaxTokens] = useState(
    agent.maxTokens != null ? String(agent.maxTokens) : "",
  );
  const [effort, setEffort] = useState(agent.effort ?? "");
  const [rawParams, setRawParams] = useState<RawParamPair[]>(() => extraToPairs(agent.extra));
  const [maxSteps, setMaxSteps] = useState(agent.maxSteps != null ? String(agent.maxSteps) : "");
  // Route param change remounts this whole page in the common case, but
  // nothing here guarantees it -- a prior bug in this exact codebase (the
  // Chat window carrying a previous agent's session across a switch) showed
  // that assumption can quietly fail. Sync local edit state to the loaded
  // agent explicitly instead of trusting a remount to reset it.
  useEffect(() => {
    setTemperature(agent.temperature != null ? String(agent.temperature) : "");
    setMaxTokens(agent.maxTokens != null ? String(agent.maxTokens) : "");
    setEffort(agent.effort ?? "");
    setRawParams(extraToPairs(agent.extra));
    setMaxSteps(agent.maxSteps != null ? String(agent.maxSteps) : "");
    // agent.extra is a fresh object reference on every fetch; depending on it
    // directly would re-run this on every render and clobber in-progress
    // edits, so this re-syncs on agent identity (agent.id) instead.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.id, agent.temperature, agent.maxTokens, agent.effort, agent.maxSteps]);

  function saveSamplingOverrides() {
    if (!agent.modelConfigId) return;
    const trimmedTemp = temperature.trim();
    const parsedTemp = trimmedTemp ? Number(trimmedTemp) : null;
    if (parsedTemp !== null && !Number.isFinite(parsedTemp)) {
      toast.error(t("Temperature must be a number", "Temperature muss eine Zahl sein"));
      return;
    }
    const trimmedTokens = maxTokens.trim();
    const parsedTokens = trimmedTokens ? Number(trimmedTokens) : null;
    if (parsedTokens !== null && (!Number.isInteger(parsedTokens) || parsedTokens < 1)) {
      toast.error(
        t(
          "Max tokens must be a positive whole number",
          "Max. Tokens muss eine positive Ganzzahl sein",
        ),
      );
      return;
    }
    const trimmedSteps = maxSteps.trim();
    const parsedSteps = trimmedSteps ? Number(trimmedSteps) : null;
    if (parsedSteps !== null && (!Number.isInteger(parsedSteps) || parsedSteps < 1)) {
      toast.error(
        t(
          "Max steps must be a positive whole number",
          "Max. Steps muss eine positive Ganzzahl sein",
        ),
      );
      return;
    }
    switchModel.mutate(
      {
        agentId: agent.id,
        modelConfigId: agent.modelConfigId,
        temperature: parsedTemp,
        maxTokens: parsedTokens,
        // Never send an override for a field this model's provider doesn't
        // expose -- switching away from anthropic must not silently wipe an
        // effort value the operator can no longer even see to restore.
        effort: showEffort ? effort.trim() || null : undefined,
        extra: showRawParams ? (pairsToExtra(rawParams) ?? null) : undefined,
        maxSteps: parsedSteps,
      },
      {
        onSuccess: () =>
          toast.success(t("Sampling settings saved", "Sampling-Einstellungen gespeichert")),
        onError: (error) => toast.error(error.message),
      },
    );
  }

  return (
    <Panel className="p-5">
      <ConfigSectionHeader
        hint={t("reasoning engine", "Denkmaschine")}
        title={t("Assigned LLM", "Zugewiesenes LLM")}
      />
      {/* Third of the three surfaces the design's Global Constraints name for
          this badge (provider tile, models table row, agent detail). It sits
          beside the select rather than inside it -- an <option> cannot hold
          markup -- and labels what THIS agent is actually assigned, which is
          the fact that matters on this page. */}
      {assigned?.provider === SUBSCRIPTION_PROVIDER && (
        <div className="mt-3">
          <SubscriptionRiskBadge />
        </div>
      )}
      <select
        className="mt-4 w-full rounded-md border border-border bg-background/30 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
        value={agent.modelConfigId ?? ""}
        disabled={!mayManage || switchModel.isPending || models.length === 0}
        aria-label={t("Assigned LLM", "Zugewiesenes LLM")}
        onChange={(event) => {
          const modelConfigId = event.target.value;
          if (!modelConfigId || modelConfigId === agent.modelConfigId) return;
          switchModel.mutate(
            { agentId: agent.id, modelConfigId },
            {
              onSuccess: () => toast.success(t("Model updated", "Modell aktualisiert")),
              onError: (error) => toast.error(error.message),
            },
          );
        }}
      >
        <option value="" disabled>
          {t("Select a model", "Modell auswählen")}
        </option>
        {models.map((model) => (
          <option key={model.id} value={model.id}>
            {model.displayName || model.model} · {model.provider}
          </option>
        ))}
      </select>
      <p className="mt-2 text-[11px] text-muted-foreground">
        {t(
          "Changing the model takes effect for the next run. Manage model configurations under Models.",
          "Die Änderung gilt für den nächsten Lauf. Modellkonfigurationen verwaltest du unter Modelle.",
        )}
        {!mayManage &&
          ` ${t("Your role does not include agent:manage, so this assignment is read-only for you.", "Ihre Rolle enthält agent:manage nicht, deshalb ist diese Zuweisung für Sie schreibgeschützt.")}`}
      </p>

      {agent.modelConfigId && (
        <div className="mt-5 border-t border-border pt-4">
          <ConfigSectionHeader
            hint={t("per-agent, optional", "pro Agent, optional")}
            title={t("Sampling overrides", "Sampling-Overrides")}
          />
          <p className="mt-1 text-[11px] text-muted-foreground">
            {t(
              "Blank fields inherit this agent's assigned model's own setting.",
              "Leere Felder übernehmen die Einstellung des zugewiesenen Modells.",
            )}
          </p>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                {t("Temperature", "Temperature")}
              </span>
              <input
                type="text"
                inputMode="decimal"
                placeholder={t("Inherited", "Geerbt")}
                value={temperature}
                disabled={!mayManage}
                onChange={(e) => setTemperature(e.target.value)}
                className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                {t("Max tokens", "Max. Tokens")}
              </span>
              <input
                type="text"
                inputMode="numeric"
                placeholder={t("Inherited", "Geerbt")}
                value={maxTokens}
                disabled={!mayManage}
                onChange={(e) => setMaxTokens(e.target.value)}
                className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
              />
            </label>
            {showEffort && (
              <label className="col-span-2 block">
                <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Effort", "Effort")}
                </span>
                <input
                  type="text"
                  placeholder={t("Inherited", "Geerbt")}
                  value={effort}
                  disabled={!mayManage}
                  onChange={(e) => setEffort(e.target.value)}
                  className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
                />
              </label>
            )}
          </div>
          {showRawParams && (
            <div className="mt-3">
              <RawParamsEditor pairs={rawParams} onChange={setRawParams} disabled={!mayManage} />
            </div>
          )}
          <div className="mt-3 border-t border-border pt-3">
            <label className="block max-w-[calc(50%-0.375rem)]">
              <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
                {t("Max steps per run", "Max. Steps pro Lauf")}
              </span>
              <input
                type="text"
                inputMode="numeric"
                placeholder={t("Framework default", "Framework-Standard")}
                value={maxSteps}
                disabled={!mayManage}
                onChange={(e) => setMaxSteps(e.target.value)}
                className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
              />
            </label>
            <p className="mt-1 text-[11px] text-muted-foreground">
              {t(
                "How many tool-using steps this agent may take in one run before it stops. Blank inherits the framework default (currently high, not unlimited -- budget/token metering only checks once per run).",
                "Wie viele Tool-Schritte dieser Agent pro Lauf ausführen darf, bevor er stoppt. Leer übernimmt den Framework-Standard (aktuell hoch, aber nicht unbegrenzt -- Budget-/Token-Metering wird nur einmal pro Lauf geprüft).",
              )}
            </p>
          </div>
          {mayManage && (
            <button
              type="button"
              onClick={saveSamplingOverrides}
              disabled={switchModel.isPending}
              className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-accent disabled:opacity-50"
            >
              {t("Save sampling settings", "Sampling-Einstellungen speichern")}
            </button>
          )}
        </div>
      )}
    </Panel>
  );
}

// The agent's tool-access + guardrails panel, merged into one table on
// Task 12: enable/disable, login pin, and fine-grained permissions
// (read/modify/approval-€/presets) all live here now, one row per tool,
// via the shared ToolGuardrailTable (Task 11). Persists via PUT
// /agents/{id}/narrowing (useUpdateNarrowing) as
// { narrowing: { tools: { <key>: {...} } } }, rebuilding the full tools
// payload from agent.effectiveTools/narrowingTools so a field a given save
// doesn't itself edit survives untouched -- same REPLACE semantics the two
// deleted panels (AgentToolAccessPanel/NarrowingEditor) relied on.
export function AgentGuardrailsPanel({
  agent,
  mayManage,
}: {
  agent: AgentDetailData;
  mayManage: boolean;
}) {
  const t = useT();
  const update = useUpdateNarrowing(agent.id);
  const logins = useMcpLogins();
  const { data: connections = [] } = useMcpConnections();
  const createLogin = useCreateMcpLogin();
  const frame = agent.departmentFrameTools;
  const narrowing = agent.narrowingTools;
  const effective = agent.effectiveTools;
  const frameKeys = Object.keys(frame).filter((k) => frame[k]?.enabled);
  const agentOnlyKeys = Object.keys(effective).filter((k) => !(k in frame));
  const allKeys = [...frameKeys, ...agentOnlyKeys];
  const connectionByName = new Map(connections.map((c) => [c.name, c] as const));
  // Only logins this agent's own department could use: tenant-wide
  // (departmentId === null) plus ones scoped to this agent's department --
  // never another department's login sharing this tool key (agent tool
  // login selection design's department-scoping extension).
  const loginsByKey: Record<string, McpLoginDTO[]> = {};
  for (const login of logins.data ?? []) {
    if (login.departmentId !== null && login.departmentId !== agent.departmentId) continue;
    (loginsByKey[login.name] ??= []).push(login);
  }
  const frameToolKeys = Object.keys(frame);
  const addableNames = Array.from(new Set(connections.map((c) => c.name))).filter(
    (name) => !allKeys.includes(name) && !frameToolKeys.includes(name),
  );

  function toGuardrailValue(key: string): GuardrailValue {
    const src = narrowing[key] ?? frame[key];
    return {
      read: !!src?.read,
      modify: !!src?.modify,
      approvalActions: src?.approvalActions ?? [],
      approvalEur: src?.approvalEur ?? null,
      only: src?.only ?? [],
      conditions: src?.conditions ?? [],
    };
  }

  // `connectionIdOverride` lets the login-picker onChange below (which
  // sets a NEW connection_id, not a GuardrailValue field) share this same
  // save path instead of needing a second, parallel save function --
  // `undefined` means "leave whatever connection_id this tool already
  // has," `null` means "clear it," a string means "set it to this."
  function persistOne(
    key: string,
    next: GuardrailValue,
    connectionIdOverride?: string | null,
  ): Promise<boolean> {
    const keys = new Set(allKeys);
    keys.add(key);
    const tools: Record<string, unknown> = {};
    for (const k of keys) {
      const isEdited = k === key;
      const src = narrowing[k] ?? frame[k];
      const val = isEdited ? next : toGuardrailValue(k);
      tools[k] = {
        enabled: isEdited ? true : !!src?.enabled,
        read: val.read,
        modify: val.modify,
        approval_eur: val.approvalEur,
        approval_actions: val.approvalActions,
        only: val.only,
        conditions: val.conditions,
        connection_id:
          isEdited && connectionIdOverride !== undefined
            ? connectionIdOverride
            : (src?.connectionId ?? null),
      };
    }
    return new Promise((resolve) => {
      update.mutate(
        { narrowing: { tools } },
        {
          onSuccess: () => {
            toast.success(t("Guardrails saved", "Guardrails gespeichert"), {
              description: agent.name,
            });
            resolve(true);
          },
          onError: (error) => {
            toast.error(describeGuardrailSaveError(error, t));
            resolve(false);
          },
        },
      );
    });
  }

  function addTool(name: string, policy: GuardrailValue | null, connectionId?: string | null) {
    persistOne(
      name,
      policy ?? {
        read: true,
        modify: false,
        approvalActions: [],
        approvalEur: null,
        only: [],
        conditions: [],
      },
      connectionId,
    );
  }

  const rows = allKeys.map((key) => {
    const isAgentOnly = agentOnlyKeys.includes(key);
    const own = toGuardrailValue(key);
    const ceiling: GuardrailValue | null = isAgentOnly
      ? null
      : {
          read: !!frame[key]?.read,
          modify: !!frame[key]?.modify,
          approvalActions: frame[key]?.approvalActions ?? [],
          approvalEur: frame[key]?.approvalEur ?? null,
          only: frame[key]?.only ?? [],
          conditions: frame[key]?.conditions ?? [],
        };
    // Mere presence in `narrowing` is NOT the signal -- every save rewrites
    // every currently-relevant key whether or not it was the one edited, so
    // a key can appear there without the agent ever having deliberately
    // diverged. `narrowingOverriddenKeys` is the one place that's tracked
    // explicitly (see its own doc comment on AgentDetail).
    const hasOverride = agent.narrowingOverriddenKeys?.includes(key) ?? false;
    const status: "inherited" | "narrowed" | "agent-only" = isAgentOnly
      ? "agent-only"
      : hasOverride
        ? "narrowed"
        : "inherited";
    const connection = connectionByName.get(key);
    const pickedCredentialId =
      (loginsByKey[key] ?? []).find((l) => l.id === effective[key]?.connectionId)?.credentialId ??
      "";
    return {
      toolKey: key,
      connection,
      ceilingPolicy: ceiling,
      ownValue: own,
      status,
      loginPicker:
        connection?.credentialType && mayManage ? (
          <CredentialPicker
            credentialType={connection.credentialType}
            value={pickedCredentialId}
            onChange={async (credentialId) => {
              if (!credentialId) {
                persistOne(key, own, null);
                return;
              }
              const existing = (loginsByKey[key] ?? []).find(
                (l) => l.credentialId === credentialId,
              );
              if (existing) {
                persistOne(key, own, existing.id);
                return;
              }
              try {
                const login = await createLogin.mutateAsync({
                  name: key,
                  credentialType: connection.credentialType!,
                  credentialId,
                  scopes: [],
                  // Scoped to this agent's own department, not tenant-wide:
                  // lets a different department pin a different login under
                  // the same tool key instead of hitting the "already
                  // exists" conflict a tenant-wide login would.
                  departmentId: agent.departmentId ?? undefined,
                });
                persistOne(key, own, login.id);
              } catch (err) {
                toast.error(
                  t(
                    "Could not link this credential",
                    "Anmeldedaten konnten nicht verknüpft werden",
                  ),
                  { description: err instanceof Error ? err.message : String(err) },
                );
              }
            }}
          />
        ) : undefined,
    };
  });

  return (
    <Panel className="p-5">
      <ConfigSectionHeader
        hint={t("access and tools", "Zugriff und Tools")}
        title={t("Tools", "Tools")}
      />
      {allKeys.length === 0 && (
        <p className="mt-3 rounded-md border border-dashed border-border/70 bg-background/30 p-3 text-center text-xs text-muted-foreground">
          {t(
            "No tools yet — add one directly for this agent, or grant it to the whole department first.",
            "Noch keine Tools — direkt für diesen Agenten hinzufügen oder zuerst der ganzen Abteilung gewähren.",
          )}
        </p>
      )}
      {/* Always rendered, even with zero rows: its own "Add tool" button
          (bottom of the table) is otherwise the only way to grant this agent
          its very first tool, and hiding the whole table for that case hid
          the button along with it. */}
      <div className="mt-3">
        <ToolGuardrailTable
          level="agent"
          rows={rows}
          addableNames={addableNames}
          connections={connections}
          onSave={persistOne}
          onAdd={addTool}
          saving={update.isPending}
          departmentId={agent.departmentId}
          agentId={agent.id}
        />
      </div>
    </Panel>
  );
}

// ---------- Supervisor ("Teamleiter") ----------

function SupervisorPanel({ agent }: { agent: AgentDetailData }) {
  const t = useT();
  const { data: backendSup } = useAgentSupervisor(agent.id);
  const setSupervisor = useSetAgentSupervisor();
  // Supervisor-candidate picker over the whole tenant, not a paginated list
  // view.
  const { data: allAgentsPage } = useAgents({ pageSize: 200 });
  const allAgents = allAgentsPage?.items ?? [];
  // Candidate supervisors = other agents in the same department (real data).
  const candidates = allAgents.filter(
    (a) => agent.departmentId != null && a.departmentId === agent.departmentId && a.id !== agent.id,
  );
  const [value, setValue] = useState<string>("auto");
  const [interventions, setInterventions] = useState({
    watchReasoning: true,
    driftGuard: true,
    roleAudit: true,
    weeklyReport: false,
  });

  // Sync from the backend supervision assignment once loaded.
  useEffect(() => {
    if (backendSup?.supervisorAgentId) setValue(`agent:${backendSup.supervisorAgentId}`);
    if (backendSup) setInterventions((i) => ({ ...i, driftGuard: backendSup.driftGuard }));
  }, [backendSup?.supervisorAgentId, backendSup?.driftGuard, backendSup]);

  const current = resolveSupervisor(value, candidates);

  function persist(next: string, driftGuard: boolean) {
    const supervisorAgentId = next.startsWith("agent:") ? next.slice("agent:".length) : null;
    setSupervisor.mutate({ agentId: agent.id, supervisorAgentId, driftGuard });
  }

  function onChange(next: string) {
    setValue(next);
    const resolved = resolveSupervisor(next, candidates);
    persist(next, interventions.driftGuard);
    toast.success(t("Supervisor updated", "Vorgesetzter aktualisiert"), {
      description:
        resolved.kind === "agent"
          ? `${agent.name} → ${resolved.agent.name}`
          : `${agent.name} → ${t("Human oversight", "Menschliche Aufsicht")}`,
    });
  }

  return (
    <Panel className="relative overflow-hidden p-5">
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background: `radial-gradient(ellipse at top right, color-mix(in oklab, ${agent.avatarColor} 12%, transparent), transparent 65%)`,
        }}
      />
      <div className="relative">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {t("Team lead · oversight", "Teamleiter · Aufsicht")}
            </div>
            <h3 className="font-serif text-lg">
              {t("Who watches the thinking of", "Wer überwacht das Denken von")}{" "}
              <span style={{ color: agent.avatarColor }}>{agent.name}</span>?
            </h3>
            <p className="mt-1 max-w-xl text-xs text-muted-foreground">
              {t(
                "The team lead observes reasoning traces and daily behavior to prevent role drift — separate from human-in-the-loop approvals, which stay off-agent.",
                "Der Teamleiter beobachtet Denkspuren und tägliches Verhalten, um Rollen-Drift zu verhindern — getrennt von Human-in-the-Loop-Approvals, die weiterhin off-agent bleiben.",
              )}
            </p>
          </div>
          <span className="rounded-full border border-border bg-background/40 px-2 py-1 text-[11px] text-muted-foreground">
            {current.kind === "agent"
              ? current.source === "department-lead"
                ? t("via department lead", "über Abteilungsleiter")
                : t("explicit", "explizit")
              : t("human reviewer", "menschlicher Reviewer")}
          </span>
        </div>

        <div
          className="mt-4 rounded-xl border-2 border-dashed p-4"
          style={{
            borderColor: `color-mix(in oklab, ${agent.avatarColor} 45%, transparent)`,
            background: `color-mix(in oklab, ${agent.avatarColor} 6%, transparent)`,
          }}
        >
          <div className="flex flex-wrap items-center gap-3">
            <SupervisorChip supervisor={current} />
            <span className="text-[11px] text-muted-foreground">
              {t("supervises →", "beaufsichtigt →")}
            </span>
            <div className="flex items-center gap-2 rounded-md border border-border bg-background/60 px-2 py-1.5 text-xs">
              <div
                className="grid h-5 w-5 place-items-center rounded-full font-serif text-[10px] text-black"
                style={{ background: agent.avatarColor }}
              >
                {agent.name[0]}
              </div>
              <span className="font-medium text-foreground">{agent.name}</span>
              <span className="text-muted-foreground">· {agent.role}</span>
            </div>
          </div>

          <div className="mt-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
            <div>
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                {t("Assign supervisor", "Vorgesetzten zuweisen")}
              </div>
              <select
                value={value}
                onChange={(e) => onChange(e.target.value)}
                className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
              >
                <option value="auto">
                  {t("Auto (department lead)", "Automatisch (Abteilungsleiter)")}
                </option>
                <option value="human">{t("Human reviewer", "Menschlicher Reviewer")}</option>
                {candidates.length > 0 && (
                  <optgroup label={t("Agents", "Agenten")}>
                    {candidates.map((c) => (
                      <option key={c.id} value={`agent:${c.id}`}>
                        {c.name} — {c.role}
                        {c.isLead ? " · lead" : ""}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                {t(
                  "Oversight rules — reasoning & behavior",
                  "Überwachungs-Regeln — Denken & Verhalten",
                )}
              </div>
              <div className="mt-1 space-y-1.5 rounded-md border border-border bg-background/30 p-2">
                <InterventionRow
                  label={t("Observe reasoning traces", "Denkspuren beobachten")}
                  on={interventions.watchReasoning}
                  onChange={(v) => setInterventions((s) => ({ ...s, watchReasoning: v }))}
                />
                <InterventionRow
                  label={t(
                    "Drift guard — pause on off-role actions",
                    "Drift-Schutz — pausieren bei rollenfremden Aktionen",
                  )}
                  on={interventions.driftGuard}
                  onChange={(v) => {
                    setInterventions((s) => ({ ...s, driftGuard: v }));
                    persist(value, v);
                  }}
                />
                <InterventionRow
                  label={t(
                    "Daily role audit — compare actions vs. role scope",
                    "Tägliches Rollen-Audit — Aktionen vs. Rollenumfang",
                  )}
                  on={interventions.roleAudit}
                  onChange={(v) => setInterventions((s) => ({ ...s, roleAudit: v }))}
                />
                <InterventionRow
                  label={t("Weekly oversight report", "Wöchentlicher Aufsichtsbericht")}
                  on={interventions.weeklyReport}
                  onChange={(v) => setInterventions((s) => ({ ...s, weeklyReport: v }))}
                />
              </div>
            </div>
          </div>

          <div className="mt-3 flex items-start gap-2 text-[11px] text-muted-foreground">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
            <span>
              {t(
                "Prevents agent-driven drift: the supervisor watches reasoning and behavior. Approvals stay off-agent with a human-in-the-loop.",
                "Verhindert Agent-Drift: Der Teamleiter beobachtet Denken und Verhalten. Approvals bleiben off-agent per Human-in-the-Loop.",
              )}
            </span>
          </div>
        </div>
      </div>
    </Panel>
  );
}

function resolveSupervisor(value: string, candidates: Agent[]): Supervisor {
  if (value.startsWith("agent:")) {
    const id = value.slice("agent:".length);
    const a = candidates.find((c) => c.id === id);
    if (a) return { kind: "agent", agent: a, source: "explicit" };
  }
  // "auto" resolves to the department lead among the real candidates, if any.
  if (value === "auto") {
    const lead = candidates.find((c) => c.isLead);
    if (lead) return { kind: "agent", agent: lead, source: "department-lead" };
  }
  return { kind: "human", source: value === "human" ? "explicit" : "fallback" };
}

function SupervisorChip({ supervisor }: { supervisor: Supervisor }) {
  if (supervisor.kind === "human") {
    return (
      <div className="inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-2 py-1.5 text-xs">
        <div className="grid h-5 w-5 place-items-center rounded-full bg-primary/20 text-primary">
          <UserCheck className="h-3 w-3" />
        </div>
        <span className="font-medium text-foreground">Human reviewer</span>
        <span className="rounded-full border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-primary">
          thinking review
        </span>
      </div>
    );
  }
  const a = supervisor.agent;
  return (
    <div className="inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/[0.06] px-2 py-1.5 text-xs">
      <div
        className="grid h-5 w-5 place-items-center rounded-full font-serif text-[10px] text-black"
        style={{ background: a.avatarColor }}
      >
        {a.name[0]}
      </div>
      <span className="font-medium text-foreground">{a.name}</span>
      <span className="text-muted-foreground">· {a.role}</span>
      {a.isLead && (
        <span className="rounded-full border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-primary">
          lead
        </span>
      )}
    </div>
  );
}

function InterventionRow({
  label,
  on,
  onChange,
}: {
  label: string;
  on: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-3 rounded-md px-2 py-1 text-xs text-foreground/90 hover:bg-background/40">
      <span>{label}</span>
      <Toggle on={on} onChange={onChange} />
    </label>
  );
}

function ConfigSectionHeader({ hint, title }: { hint: string; title: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{hint}</div>
      <h3 className="mt-0.5 font-serif text-lg">{title}</h3>
    </div>
  );
}

// ---------- Schedule / trigger ----------

/** Which trigger TYPE this agent uses: a cron schedule (ScheduleEditor,
 * unchanged below) or a generic webhook (WebhookTriggerPanel). Defaults to
 * whichever the agent already has configured, cron otherwise -- an agent
 * genuinely has at most one of each kind today (single-mcp_conn dispatch's
 * own limit, runtime/executor.py), so "which tab is open" and "which
 * trigger exists" stay in lockstep. */
function TriggerEditor({
  initial,
  agentName,
  agentId,
}: {
  initial: string;
  agentName: string;
  agentId: string;
}) {
  const t = useT();
  const { data: triggers } = useAgentTriggers(agentId);
  const webhookTrigger = triggers?.find((tr) => tr.kind === "webhook");
  const [tab, setTab] = useState<"cron" | "webhook">(webhookTrigger ? "webhook" : "cron");

  useEffect(() => {
    if (webhookTrigger) setTab("webhook");
  }, [webhookTrigger?.id]);

  return (
    <div className="space-y-3">
      <div className="flex gap-1.5">
        <button
          type="button"
          onClick={() => setTab("cron")}
          className={cn(
            "rounded-full border px-2.5 py-1 text-[11px] transition",
            tab === "cron"
              ? "border-primary/60 bg-primary/15 text-primary"
              : "border-border bg-background/40 text-muted-foreground hover:text-foreground",
          )}
        >
          <Clock className="mr-1 inline h-3 w-3" />
          {t("Schedule", "Zeitplan")}
        </button>
        <button
          type="button"
          onClick={() => setTab("webhook")}
          className={cn(
            "rounded-full border px-2.5 py-1 text-[11px] transition",
            tab === "webhook"
              ? "border-primary/60 bg-primary/15 text-primary"
              : "border-border bg-background/40 text-muted-foreground hover:text-foreground",
          )}
        >
          <Webhook className="mr-1 inline h-3 w-3" />
          {t("Webhook", "Webhook")}
        </button>
      </div>
      {tab === "cron" ? (
        <ScheduleEditor initial={initial} agentName={agentName} agentId={agentId} />
      ) : (
        <WebhookTriggerPanel agentId={agentId} agentName={agentName} trigger={webhookTrigger} />
      )}
    </div>
  );
}

/** Generic, n8n-Webhook-node-style trigger: one unguessable URL, any caller,
 * any JSON payload -- no per-source setup (see backend/src/oc8/api/v1/
 * webhooks.py). The URL is not one-time-reveal; it is fetched fresh every
 * time this agent's triggers list loads, so an operator can always come
 * back and re-copy it into the external system's config. */
function WebhookTriggerPanel({
  agentId,
  agentName,
  trigger,
}: {
  agentId: string;
  agentName: string;
  trigger: TriggerDTO | undefined;
}) {
  const t = useT();
  const createTrigger = useCreateAgentTrigger();
  const deleteTrigger = useDeleteAgentTrigger();
  const [copied, setCopied] = useState(false);

  function create() {
    createTrigger.mutate(
      {
        agentId,
        kind: "webhook",
        taskText: t(
          `React to whatever this webhook sends, on ${agentName}'s behalf.`,
          `Reagiere auf das, was dieser Webhook sendet, im Namen von ${agentName}.`,
        ),
      },
      {
        onSuccess: () => toast.success(t("Webhook created", "Webhook erstellt")),
        onError: () =>
          toast.error(t("Couldn't create webhook", "Webhook konnte nicht erstellt werden")),
      },
    );
  }

  function remove() {
    if (!trigger) return;
    deleteTrigger.mutate(
      { agentId, triggerId: trigger.id },
      {
        onSuccess: () => toast.success(t("Webhook removed", "Webhook entfernt")),
        onError: () =>
          toast.error(t("Couldn't remove webhook", "Webhook konnte nicht entfernt werden")),
      },
    );
  }

  async function copy() {
    if (!trigger?.webhookUrl) return;
    await navigator.clipboard.writeText(trigger.webhookUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  if (!trigger) {
    return (
      <div className="space-y-3">
        <p className="text-[11px] text-muted-foreground">
          {t(
            "Any system that can send a JSON POST can trigger this agent — no signature or per-source setup needed. The URL itself is the secret, so it isn't shown to anyone who isn't looking at this page.",
            "Jedes System, das per POST JSON senden kann, kann diesen Agenten auslösen — keine Signatur oder Quellen-spezifische Einrichtung nötig. Die URL selbst ist das Geheimnis und wird nur hier angezeigt.",
          )}
        </p>
        <button
          type="button"
          onClick={create}
          disabled={createTrigger.isPending}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Webhook className="h-3.5 w-3.5" />
          {createTrigger.isPending
            ? t("Creating…", "Wird erstellt…")
            : t("Create webhook URL", "Webhook-URL erstellen")}
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 rounded-md border border-border bg-background/30 px-3 py-2">
        <code className="min-w-0 flex-1 truncate font-mono text-xs">{trigger.webhookUrl}</code>
        <button
          type="button"
          onClick={copy}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:text-foreground"
        >
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {copied ? t("Copied", "Kopiert") : t("Copy", "Kopieren")}
        </button>
      </div>
      <p className="text-[11px] text-muted-foreground">
        {t(
          "Paste this into the external system's webhook / automation config — e.g. Odoo's Automation Rules → Send Webhook Notification.",
          "Trage diese URL in die Webhook-/Automatisierungs-Konfiguration des externen Systems ein — z. B. Odoos Automation Rules → Send Webhook Notification.",
        )}
      </p>
      <button
        type="button"
        onClick={remove}
        disabled={deleteTrigger.isPending}
        className="inline-flex items-center gap-1 text-[11px] text-muted-foreground transition hover:text-foreground disabled:opacity-50"
      >
        <Trash2 className="h-3 w-3" />
        {t("Remove webhook", "Webhook entfernen")}
      </button>
    </div>
  );
}

type TriggerMode = "continuous" | "24-7" | "schedule" | "on-demand" | "custom";

// "schedule" (free-text "Weekly schedule") dropped from the preset list --
// Custom cron already covers everything it could express, so it was a
// redundant option. `detectTriggerMode` below still falls back to it for
// an agent whose `agent.schedule` already holds an old free-text value, so
// that legacy data keeps rendering rather than being reinterpreted as cron.
const TRIGGER_PRESETS: { id: TriggerMode; label: string; example: string }[] = [
  { id: "continuous", label: "Continuous", example: "Continuous" },
  { id: "24-7", label: "24/7", example: "24/7" },
  { id: "on-demand", label: "On-Demand", example: "On-Demand" },
  { id: "custom", label: "Time scheduled", example: "0 */2 * * *" },
];

function detectTriggerMode(value: string): TriggerMode {
  const v = value.trim().toLowerCase();
  if (v === "continuous") return "continuous";
  if (v === "24/7") return "24-7";
  if (v === "on-demand") return "on-demand";
  if (/^[0-9*/,\-\s]+$/.test(v)) return "custom";
  return "schedule";
}

function ScheduleEditor({
  initial,
  agentName,
  agentId,
}: {
  initial: string;
  agentName: string;
  agentId: string;
}) {
  const t = useT();
  const { data: triggers } = useAgentTriggers(agentId);
  const cronTrigger = triggers?.find((tr) => tr.kind === "cron");
  const createTrigger = useCreateAgentTrigger();
  const updateTrigger = useUpdateAgentTrigger();
  const deleteTrigger = useDeleteAgentTrigger();
  const [mode, setMode] = useState<TriggerMode>(() => detectTriggerMode(initial));
  const [value, setValue] = useState<string>(initial);

  useEffect(() => {
    if (cronTrigger?.cronExpression) {
      setMode("custom");
      setValue(cronTrigger.cronExpression);
    }
  }, [cronTrigger?.cronExpression]);

  function pickMode(next: TriggerMode) {
    setMode(next);
    const preset = TRIGGER_PRESETS.find((p) => p.id === next)!;
    if (next !== "schedule" && next !== "custom") {
      setValue(preset.example);
    }
    if (next === "custom") {
      // Seed a valid cron if the current value is not one.
      const isCron = /^[0-9*/,\-\s]+$/.test(value.trim());
      if (!isCron) setValue("0 9 * * 1-5");
    }
  }

  function save() {
    const onFailed = () =>
      toast.error(t("Couldn't update schedule", "Zeitplan konnte nicht aktualisiert werden"), {
        description: t("Try again in a moment.", "Bitte versuche es in Kürze erneut."),
      });
    // Only "custom" mode produces a real cron string (via CronBuilder); the
    // other presets are display-only and never touch the backend.
    if (mode !== "custom") {
      const onDone = () =>
        toast.success(t("Schedule updated", "Zeitplan aktualisiert"), {
          description: `${agentName} · ${value || TRIGGER_PRESETS.find((p) => p.id === mode)?.example || mode}`,
        });
      if (cronTrigger) {
        deleteTrigger.mutate(
          { agentId, triggerId: cronTrigger.id },
          { onSuccess: onDone, onError: onFailed },
        );
      } else {
        onDone();
      }
      return;
    }
    const onSaved = () =>
      toast.success(t("Schedule updated", "Zeitplan aktualisiert"), {
        description: `${agentName} · ${value}`,
      });
    if (cronTrigger) {
      updateTrigger.mutate(
        { agentId, triggerId: cronTrigger.id, cronExpression: value },
        { onSuccess: onSaved, onError: onFailed },
      );
    } else {
      createTrigger.mutate(
        { agentId, cronExpression: value, taskText: `Scheduled run for ${agentName}` },
        { onSuccess: onSaved, onError: onFailed },
      );
    }
  }

  const showTextInput = mode === "schedule" || mode === "custom";

  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {TRIGGER_PRESETS.map((p) => {
          const active = p.id === mode;
          return (
            <button
              key={p.id}
              type="button"
              onClick={() => pickMode(p.id)}
              className={cn(
                "rounded-full border px-2.5 py-1 text-[11px] transition",
                active
                  ? "border-primary/60 bg-primary/15 text-primary"
                  : "border-border bg-background/40 text-muted-foreground hover:text-foreground",
              )}
            >
              {p.label}
            </button>
          );
        })}
      </div>
      {showTextInput ? (
        mode === "custom" ? (
          <CronBuilder
            initial={value}
            onChange={setValue}
            onSave={save}
            saveLabel={t("Save", "Speichern")}
          />
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="z. B. Mon–Fri, 09:00–17:00"
              className="min-w-0 flex-1 rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
            <button
              type="button"
              onClick={save}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
            >
              <Save className="h-3.5 w-3.5" /> {t("Save", "Speichern")}
            </button>
          </div>
        )
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-background/30 px-3 py-2 text-sm">
          <span className="font-mono text-xs text-muted-foreground">{value}</span>
          <button
            type="button"
            onClick={save}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition hover:opacity-90"
          >
            <Save className="h-3.5 w-3.5" /> {t("Save", "Speichern")}
          </button>
        </div>
      )}
      <p className="text-[11px] text-muted-foreground">
        {t(
          "Pick a preset or define an exact schedule / cron expression.",
          "Wähle einen Preset oder definiere einen exakten Zeitplan / Cron-Ausdruck.",
        )}
      </p>
    </div>
  );
}

// ---------- Files ----------
//
// Every file any of this agent's runs has produced (write_output_file, or a
// synced /workspace/output/ mount -- see runtime/workspace.py), across every
// runtime. GET /agents/{id}/workspace/files lists them; content is fetched
// via the shared GET /files/{id} route (downloadFileAttachment), the same
// route chat/instruction attachments use.

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Text-ish content types get an inline preview; everything else (pdf, docx,
// xlsx, images) only offers Download.
const _PREVIEWABLE_CONTENT_TYPES = new Set([
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/html",
]);

// Capped client-side so one huge text file can't pin the tab's memory --
// mirrors the spirit of the old server-side _MAX_WORKSPACE_FILE_BYTES.
const _MAX_PREVIEW_BYTES = 2_000_000;

function WorkspaceFilesPanel({ agentId }: { agentId: string }) {
  const t = useT();
  const files = useAgentWorkspaceFiles(agentId);
  const [selected, setSelected] = useState<WorkspaceFileDTO | null>(null);
  const [preview, setPreview] = useState<{ id: string; text: string; truncated: boolean } | null>(
    null,
  );
  const [previewLoading, setPreviewLoading] = useState(false);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  async function handleSelect(f: WorkspaceFileDTO) {
    setSelected(f);
    setPreview(null);
    if (!_PREVIEWABLE_CONTENT_TYPES.has(f.contentType)) return;
    setPreviewLoading(true);
    try {
      const blob = await downloadFileAttachment(f.id);
      const full = await blob.text();
      setPreview({
        id: f.id,
        text: full.slice(0, _MAX_PREVIEW_BYTES),
        truncated: full.length > _MAX_PREVIEW_BYTES,
      });
    } catch {
      toast.error(t("Could not load file preview.", "Dateivorschau konnte nicht geladen werden."));
    } finally {
      setPreviewLoading(false);
    }
  }

  async function handleDownload(f: WorkspaceFileDTO) {
    setDownloadingId(f.id);
    try {
      const blob = await downloadFileAttachment(f.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = f.filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      toast.error(t("Download failed.", "Download fehlgeschlagen."));
    } finally {
      setDownloadingId(null);
    }
  }

  if (files.isPending) {
    return (
      <Panel className="p-10 text-center">
        <p className="text-sm text-muted-foreground">
          {t("Loading files…", "Dateien werden geladen…")}
        </p>
      </Panel>
    );
  }

  const data = files.data;
  if (!data || data.files.length === 0) {
    return (
      <Panel className="p-10 text-center">
        <FileText className="mx-auto mb-3 h-6 w-6 text-muted-foreground/60" />
        <p className="text-sm text-muted-foreground">
          {t(
            "This agent has not produced any files yet.",
            "Dieser Agent hat noch keine Dateien erzeugt.",
          )}
        </p>
      </Panel>
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,260px)_minmax(0,1fr)]">
      <Panel className="max-h-[480px] overflow-auto p-2">
        <ul className="space-y-0.5">
          {data.files.map((f) => (
            <li key={f.id}>
              <button
                type="button"
                onClick={() => handleSelect(f)}
                className={cn(
                  "flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-xs transition hover:bg-muted/60",
                  selected?.id === f.id && "bg-muted text-foreground",
                )}
                title={f.filename}
              >
                <span className="truncate font-mono">{f.filename}</span>
                <span className="shrink-0 text-[10px] text-muted-foreground">
                  {formatFileSize(f.sizeBytes)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel className="max-h-[480px] overflow-auto p-3">
        {!selected ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            {t("Select a file to view its contents.", "Datei auswählen, um den Inhalt zu sehen.")}
          </div>
        ) : (
          <>
            <div className="mb-3 flex items-center justify-between gap-2">
              <span className="truncate font-mono text-xs" title={selected.filename}>
                {selected.filename}
              </span>
              <button
                type="button"
                onClick={() => handleDownload(selected)}
                disabled={downloadingId === selected.id}
                className="flex shrink-0 items-center gap-1 rounded-md border px-2 py-1 text-[11px] hover:bg-muted/60 disabled:opacity-50"
              >
                <Download className="h-3 w-3" />
                {t("Download", "Herunterladen")}
              </button>
            </div>
            {previewLoading ? (
              <div className="py-10 text-center text-xs text-muted-foreground">
                {t("Loading…", "Wird geladen…")}
              </div>
            ) : !_PREVIEWABLE_CONTENT_TYPES.has(selected.contentType) ? (
              <div className="py-10 text-center text-xs text-muted-foreground">
                {t(
                  "No inline preview for this file type -- download it instead.",
                  "Keine Inline-Vorschau für diesen Dateityp -- bitte herunterladen.",
                )}
              </div>
            ) : (
              <>
                {preview?.truncated && (
                  <div className="mb-2 text-[11px] text-[color:var(--status-warning)]">
                    {t("File truncated for display.", "Datei für die Anzeige gekürzt.")}
                  </div>
                )}
                <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed">
                  {preview?.text ?? ""}
                </pre>
              </>
            )}
          </>
        )}
      </Panel>
    </div>
  );
}

// ---------- Live Log ----------
//
// Real live run transcript: the current run (useRun, live-patched by the
// "run.status" WS event in src/lib/live/apply-event.ts) plus this agent's
// slice of the activity feed (useActivity, live-patched by "activity.logged").
// No client-side simulation — every line here comes from the backend.

function isTerminalRunState(state: string): boolean {
  return state === "done" || state === "failed" || state === "interrupted";
}

function LiveLog({ agentId, runId }: { agentId: string; runId: string | null }) {
  const t = useT();
  const run = useRun(runId);
  // Asked of the server for THIS agent, and raised by "show more": filtering a
  // global page client-side meant a quiet agent showed an empty feed while its
  // own history sat just past the cut.
  const [feedLimit, setFeedLimit] = useState(50);
  const activity = useActivity({ agentId, limit: feedLimit });
  const agentActivity = activity.data ?? [];
  const mayHaveMore = agentActivity.length >= feedLimit;
  const live = !!runId && !!run.data && !isTerminalRunState(run.data.state);
  const cancelRun = useCancelRun(runId ?? "");
  // Cancel is cooperative (§7.2): a successful call doesn't flip `run.state`
  // itself, it only records intent -- the run stops at its next step
  // boundary and `state` eventually reads "interrupted" on its own. Track
  // "already asked" locally, keyed to THIS run, so the button disables
  // right away instead of inviting a second click while waiting; a run
  // change (new runId) clears it naturally since the comparison fails.
  const [cancelledRunId, setCancelledRunId] = useState<string | null>(null);
  const alreadyRequestedCancel = !!runId && cancelledRunId === runId;

  return (
    <div className="space-y-4">
      <Panel className="p-5">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "relative inline-flex h-2.5 w-2.5 rounded-full",
                live ? "bg-[color:var(--status-running)]" : "bg-muted-foreground/60",
              )}
            >
              {live && (
                <span className="absolute inset-0 animate-ping rounded-full bg-[color:var(--status-running)] opacity-60" />
              )}
            </span>
            <div className="text-xs uppercase tracking-wider text-muted-foreground">
              {t("Live Log · current run", "Live-Log · aktueller Lauf")}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {live && (
              <button
                type="button"
                onClick={() =>
                  cancelRun.mutate(undefined, {
                    onSuccess: () => {
                      setCancelledRunId(runId);
                      toast.success(t("Cancel requested", "Abbruch angefordert"), {
                        description: t(
                          "The run will stop at its next step boundary.",
                          "Der Lauf stoppt beim nächsten Schritt.",
                        ),
                      });
                    },
                    onError: () =>
                      toast.error(t("Couldn't cancel run", "Lauf konnte nicht abgebrochen werden")),
                  })
                }
                disabled={cancelRun.isPending || alreadyRequestedCancel}
                title={t("Stop this run", "Diesen Lauf abbrechen")}
                className="inline-flex items-center gap-1.5 rounded-md border border-[color:var(--status-error)]/40 px-2.5 py-1.5 text-xs font-medium text-[color:var(--status-error)] transition hover:bg-[color:var(--status-error)]/10 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <StopCircle className="h-3.5 w-3.5" />
                {alreadyRequestedCancel
                  ? t("Cancelling…", "Wird abgebrochen…")
                  : t("Stop", "Abbrechen")}
              </button>
            )}
            {run.data && <RunStateBadge state={run.data.state} />}
          </div>
        </div>
        {runId ? (
          <RunTranscript run={run.data} pending={run.isPending} />
        ) : (
          <div className="py-10 text-center text-xs text-muted-foreground">
            {t(
              "No run in progress. Start one with “Run now”, or wait for the schedule.",
              "Kein Lauf aktiv. Starte einen mit „Jetzt ausführen“ — oder warte auf den Zeitplan.",
            )}
          </div>
        )}
      </Panel>
      <ActivityFeed
        items={agentActivity}
        onShowMore={mayHaveMore ? () => setFeedLimit((n) => n + 50) : undefined}
      />
    </div>
  );
}

function RunTranscript({ run, pending }: { run: RunDTO | undefined; pending: boolean }) {
  const t = useT();
  if (pending) {
    return (
      <div className="py-10 text-center text-xs text-muted-foreground">
        {t("Loading run…", "Lauf wird geladen…")}
      </div>
    );
  }
  if (!run) {
    return (
      <div className="py-10 text-center text-xs text-muted-foreground">
        {t("Run not found.", "Lauf nicht gefunden.")}
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="rounded-md border border-border bg-background/40 px-2 py-1 font-mono">
          {t("run", "Lauf")} {run.id.slice(0, 8)}
        </span>
        {run.phase && (
          <span className="rounded-md border border-border bg-background/40 px-2 py-1">
            {t("phase", "Phase")}: {run.phase}
          </span>
        )}
      </div>
      {run.state === "waiting_for_input" && <WaitingForInputCallout run={run} />}
      {run.liveAnswer && (
        <LiveAnswerPane text={run.liveAnswer} live={!isTerminalRunState(run.state)} />
      )}
      {run.transcript && run.transcript.length > 0 && (
        <LiveTranscriptPane transcript={run.transcript} live={!isTerminalRunState(run.state)} />
      )}
      {run.components && run.components.length > 0 && (
        <div className="space-y-2">
          {run.components.map((c, i) => {
            const Renderer = Object.prototype.hasOwnProperty.call(
              RUN_COMPONENT_REGISTRY,
              c.componentKey,
            )
              ? RUN_COMPONENT_REGISTRY[c.componentKey]
              : undefined;
            return Renderer ? <Renderer key={i} props={c.props} /> : null;
          })}
        </div>
      )}
      {run.todos && run.todos.length > 0 && <RunTodoList todos={run.todos} />}
      <div className="max-h-[360px] min-h-[160px] overflow-auto rounded-md border border-border bg-background/60 p-3 font-mono text-[12px] leading-relaxed">
        {run.toolCalls.length === 0 ? (
          <div className="py-6 text-center text-xs text-muted-foreground">
            {t("No tool calls yet.", "Noch keine Tool-Aufrufe.")}
          </div>
        ) : (
          run.toolCalls.map((call, i) => <ToolCallRow key={i} call={call} />)
        )}
      </div>
    </div>
  );
}

// Growing pane for the model's own answer as it streams in (Stage 2 token
// streaming), fed by "run.token_delta" events (see apply-event.ts) -- the
// SAME terminal look and same scroll-position rule as LiveTranscriptPane
// below, but rendering one continuous string rather than joining an array
// of separate stdout lines, since token fragments are words of one answer,
// not log lines. Applies to every runtime: unlike run.output_delta (raw
// container stdout, container runtimes only), run.token_delta comes from
// wherever the model call itself runs -- in-process or isolated.
function LiveAnswerPane({ text, live }: { text: string; live: boolean }) {
  const t = useT();
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [text]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
  }

  return (
    <div>
      <div className="mb-1 flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted-foreground">
        {live && (
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[color:var(--status-running)]">
            <span className="absolute inset-0 animate-ping rounded-full bg-[color:var(--status-running)] opacity-60" />
          </span>
        )}
        {t("Answer", "Antwort")}
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="max-h-[280px] overflow-auto rounded-md border border-border bg-black/90 p-3 font-mono text-[11px] leading-relaxed text-emerald-300"
      >
        <pre className="whitespace-pre-wrap break-words">{text}</pre>
      </div>
    </div>
  );
}

// Growing, terminal-like output pane for a containerized runtime's raw
// stdout, fed by "run.output_delta" events (see apply-event.ts). Auto-scrolls
// to the bottom on every new chunk UNLESS the operator has scrolled up to
// read something earlier -- checked by distance-from-bottom right before the
// chunk that triggered the re-render is appended, so a deliberate scroll-back
// is never yanked back down mid-read.
function LiveTranscriptPane({ transcript, live }: { transcript: string[]; live: boolean }) {
  const t = useT();
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [transcript]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
  }

  return (
    <div>
      <div className="mb-1 flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted-foreground">
        {live && (
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[color:var(--status-running)]">
            <span className="absolute inset-0 animate-ping rounded-full bg-[color:var(--status-running)] opacity-60" />
          </span>
        )}
        {t("Output", "Ausgabe")}
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="max-h-[280px] overflow-auto rounded-md border border-border bg-black/90 p-3 font-mono text-[11px] leading-relaxed text-emerald-300"
      >
        <pre className="whitespace-pre-wrap break-words">{transcript.join("\n")}</pre>
      </div>
    </div>
  );
}

// Callout for a run suspended in `waiting_for_input` (RunStateBadge already
// renders the amber badge for this state above — this adds the answer
// affordance below it). Shows the real backend `run.question`; older runs
// that predate the field fall back to a generic prompt, but the textarea and
// submit path are identical either way.
function WaitingForInputCallout({ run }: { run: RunDTO }) {
  const t = useT();
  const answerRun = useAnswerRun(run.id);
  const [answer, setAnswer] = useState("");

  function submit() {
    const trimmed = answer.trim();
    if (!trimmed) return;
    answerRun.mutate(trimmed, {
      onSuccess: () => {
        setAnswer("");
        toast.success(t("Answer sent", "Antwort gesendet"));
      },
      onError: () => toast.error(t("Couldn't send answer", "Antwort konnte nicht gesendet werden")),
    });
  }

  return (
    <div className="rounded-md border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 p-3">
      <div className="flex items-start gap-2 text-sm text-foreground/90">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[color:var(--status-warning)]" />
        <p>
          {run.question ??
            t("The agent is waiting for your input.", "Der Agent wartet auf deine Eingabe.")}
        </p>
      </div>
      <textarea
        rows={3}
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        placeholder={t("Type your answer…", "Antwort eingeben…")}
        className="mt-3 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
      />
      <div className="mt-2 flex justify-end">
        <button
          type="button"
          onClick={submit}
          disabled={answerRun.isPending || !answer.trim()}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
        >
          <MessageSquare className="h-3.5 w-3.5" />
          {answerRun.isPending
            ? t("Sending…", "Wird gesendet…")
            : t("Send answer", "Antwort senden")}
        </button>
      </div>
    </div>
  );
}

function RunStateBadge({ state }: { state: string }) {
  const map: Record<string, { icon: React.ReactNode; color: string }> = {
    queued: {
      icon: <Clock className="h-3 w-3" />,
      color: "text-muted-foreground border-border bg-background/40",
    },
    running: {
      icon: <Play className="h-3 w-3" />,
      color:
        "text-[color:var(--status-running)] border-[color:var(--status-running)]/40 bg-[color:var(--status-running)]/10",
    },
    waiting_for_input: {
      icon: <AlertTriangle className="h-3 w-3" />,
      color:
        "text-[color:var(--status-warning)] border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10",
    },
    waiting_for_approval: {
      icon: <AlertTriangle className="h-3 w-3" />,
      color:
        "text-[color:var(--status-warning)] border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10",
    },
    done: {
      icon: <CheckCircle2 className="h-3 w-3" />,
      color: "text-primary border-primary/40 bg-primary/10",
    },
    failed: {
      icon: <XCircle className="h-3 w-3" />,
      color:
        "text-[color:var(--status-error)] border-[color:var(--status-error)]/40 bg-[color:var(--status-error)]/10",
    },
    interrupted: {
      icon: <XCircle className="h-3 w-3" />,
      color:
        "text-[color:var(--status-error)] border-[color:var(--status-error)]/40 bg-[color:var(--status-error)]/10",
    },
  };
  const m = map[state] ?? {
    icon: <Clock className="h-3 w-3" />,
    color: "text-muted-foreground border-border bg-background/40",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium",
        m.color,
      )}
    >
      {m.icon}
      {state}
    </span>
  );
}

// toolCalls is `Array<Record<string, unknown>>` with no fixed schema on the
// wire yet — render whichever recognizable keys are present and fall back to
// the raw JSON so nothing silently disappears.
// The agent's most recent todo_write call -- whole-list-replace, so this
// always reflects its current plan, not a history of every edit.
function RunTodoList({ todos }: { todos: RunTodoDTO[] }) {
  const t = useT();
  return (
    <div className="rounded-md border border-border bg-background/40 p-3">
      <div className="mb-2 text-[11px] uppercase tracking-wider text-muted-foreground">
        {t("Todos", "Todos")}
      </div>
      <ul className="space-y-1">
        {todos.map((todo, i) => (
          <li key={i} className="flex items-start gap-2 text-xs">
            {todo.status === "completed" ? (
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[color:var(--status-success)]" />
            ) : todo.status === "in_progress" ? (
              <CircleDot className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[color:var(--status-running)]" />
            ) : (
              <Circle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            )}
            <span
              className={cn(
                "min-w-0",
                todo.status === "completed" && "text-muted-foreground line-through",
              )}
            >
              {todo.content}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ToolCallRow({ call }: { call: Record<string, unknown> }) {
  const name =
    (call.tool as string | undefined) ??
    (call.name as string | undefined) ??
    (call.toolName as string | undefined) ??
    "tool call";
  const status = (call.status as string | undefined) ?? (call.state as string | undefined);
  const detail =
    (call.output as string | undefined) ??
    (call.result as string | undefined) ??
    (call.error as string | undefined) ??
    (typeof call.input === "string" ? call.input : undefined);
  return (
    <div className="flex flex-wrap items-start gap-2 border-b border-border/50 py-1 last:border-0">
      <Wrench className="mt-0.5 h-3 w-3 shrink-0 text-primary" />
      <span className="font-medium text-foreground/90">{name}</span>
      {status && (
        <span className="rounded border border-border px-1 text-[10px] uppercase text-muted-foreground">
          {status}
        </span>
      )}
      <span className="min-w-0 flex-1 truncate text-muted-foreground">
        {detail ?? JSON.stringify(call)}
      </span>
    </div>
  );
}

function ActivityFeed({ items, onShowMore }: { items: ActivityItem[]; onShowMore?: () => void }) {
  const t = useT();
  return (
    <Panel className="p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">
          {t("Activity feed", "Aktivitäts-Feed")}
        </span>
        <span className="font-mono text-[10px] text-muted-foreground">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div className="py-8 text-center text-xs text-muted-foreground">
          {t("No activity yet for this agent.", "Noch keine Aktivität für diesen Agenten.")}
        </div>
      ) : (
        <>
          <ul className="divide-y divide-border">
            {items.map((it) => (
              <ActivityRow key={it.id} item={it} />
            ))}
          </ul>
          {onShowMore && (
            <button
              type="button"
              onClick={onShowMore}
              className="mt-3 w-full rounded-md border border-border py-2 text-xs text-muted-foreground transition hover:bg-muted/40"
            >
              {t("Show older", "Ältere anzeigen")}
            </button>
          )}
        </>
      )}
    </Panel>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const t = useT();
  const color =
    item.status === "success"
      ? "var(--status-running)"
      : item.status === "warning"
        ? "var(--status-warning)"
        : item.status === "error"
          ? "var(--status-error)"
          : "var(--primary)";
  return (
    <li className="py-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span
          className="rounded-full px-2 py-0.5 text-[10px] font-medium"
          style={{
            color,
            background: `color-mix(in oklab, ${color} 12%, transparent)`,
            border: `1px solid color-mix(in oklab, ${color} 30%, transparent)`,
          }}
        >
          {item.status}
        </span>
        {item.cacheHit && (
          <span
            className="rounded-full border border-border px-2 py-0.5 text-[10px] font-medium text-muted-foreground"
            title={t(
              "Answered from the department cache, not a fresh model call",
              "Aus dem Department-Cache beantwortet, kein frischer Modell-Aufruf",
            )}
          >
            {t("cached", "aus Cache")}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-foreground/90">{item.message}</span>
        <span className="font-mono text-xs text-muted-foreground">{item.time}</span>
      </div>
      {item.detail && <p className="mt-1 text-xs text-muted-foreground">{item.detail}</p>}
    </li>
  );
}
