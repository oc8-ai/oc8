import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  BookOpen,
  Building2,
  Clock,
  Code2,
  Coins,
  Crown,
  Headphones,
  Megaphone,
  Repeat,
  Sparkles,
  Timer,
  TrendingUp,
  UserPlus,
  UserRound,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { AgentAvatar } from "@/components/agent-avatar";
import { Panel, StatusPill } from "@/components/app-shell";
import { InlineRename } from "@/components/inline-rename";
import { NewAgentDialog } from "@/components/new-agent-dialog";
import { MemoryEditor } from "@/components/memory-editor";
import { KnowledgeAssignment } from "@/components/knowledge-assignment";
import { ComponentGrantPanel } from "@/components/component-grant-panel";
import type { Condition, GuardrailValue } from "@/components/guardrail-preset-picker";
import {
  ToolGuardrailTable,
  type ToolGuardrailRow,
  describeGuardrailSaveError,
} from "@/components/tool-guardrail-table";
import { StatCard } from "@/components/stat-card";
import { contractsFor, type IntakeContract } from "@/lib/collaboration";
import { formatMs } from "@/lib/format";
import {
  useDeleteDepartment,
  useDepartment,
  useDepartmentAgents,
  useDepartmentBoard,
  useDepartmentContracts,
  useDepartmentKpis,
  useDepartmentTools,
  useSetDepartmentTools,
  useKnowledgeBases,
  useMcpConnections,
  useSetDepartmentContracts,
  useUpdateDepartment,
  type DepartmentContractsDTO,
  type IntakeDTO,
} from "@/lib/hooks";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Plus,
  ShieldCheck,
  X as XIcon,
  Zap,
} from "lucide-react";
import { type Agent, type Supervisor, type Task, type TaskColumn } from "@/lib/mock-data";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";

const iconMap = {
  sales: TrendingUp,
  engineering: Code2,
  marketing: Megaphone,
  finance: Coins,
  hr: UserRound,
  support: Headphones,
} as const;

// Pure resolver over a fetched `agents` list -- mirrors the former
// mock-data.ts supervisorOf, now parameterized on real query data instead
// of the static mock array (which never contains a real agent's live id).
function supervisorOf(agents: Agent[], agent: Agent): Supervisor {
  if (agent.supervisorId === "human") return { kind: "human", source: "explicit" };
  if (agent.supervisorId) {
    const a = agents.find((x) => x.id === agent.supervisorId);
    if (a) return { kind: "agent", agent: a, source: "explicit" };
  }
  if (agent.departmentId) {
    const lead = agents.find((a) => a.departmentId === agent.departmentId && a.isLead);
    if (lead && lead.id !== agent.id) {
      return { kind: "agent", agent: lead, source: "department-lead" };
    }
  }
  return { kind: "human", source: "fallback" };
}

export const Route = createFileRoute("/departments/$id")({
  component: DepartmentDetail,
  notFoundComponent: () => (
    <div className="p-8 text-center text-muted-foreground">Department not found.</div>
  ),
});

const nowTime = () => {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

function DepartmentDetail() {
  const t = useT();
  const { id } = Route.useParams();
  // Real identity: the id in the URL is the backend's uuid7 (see
  // departments.index.tsx), so it's resolved via the real GET
  // /departments/{id} + /departments/{id}/agents endpoints — not the mock
  // slug-keyed lookups. All hooks are called unconditionally (before the
  // loading/not-found guards below) to keep hook order stable across
  // renders; anything that needs the department itself uses `id`, not
  // `dept.id`, until the guards confirm `dept` is defined.
  const { data: dept, isLoading } = useDepartment(id);
  const { data: members = [] } = useDepartmentAgents(id);
  // Fetch up to the backend's per-column cap once; pagination below is
  // client-side over that set rather than re-fetching a growing window.
  const BOARD_FETCH_LIMIT = 200;
  const BOARD_PAGE_SIZE = 8;
  const [boardPage, setBoardPage] = useState(0);
  const { data: board } = useDepartmentBoard(id, BOARD_FETCH_LIMIT);
  // KB grants for this department, not a paginated list view.
  const { data: allBasesPage } = useKnowledgeBases({ pageSize: 200 });
  const allBases = allBasesPage?.items ?? [];
  const [tab, setTab] = useState<"overview" | "guardrails" | "collab" | "settings">("overview");
  const [hireOpen, setHireOpen] = useState(false);
  const updateDepartment = useUpdateDepartment(id);

  const taskState = board?.tasks ?? [];
  const boardTotals = board?.totals ?? {};
  const totalTasks = Object.values(boardTotals).reduce((a, b) => a + b, 0);
  // Every column paginates on the same page index, sized to whichever
  // column has the most fetched items — short columns just show fewer
  // cards on later pages rather than getting their own page count.
  const boardColumnCounts = ["backlog", "in_progress", "waiting", "done"].map(
    (col) => taskState.filter((task) => task.column === col).length,
  );
  const boardPageCount = Math.max(
    1,
    Math.ceil(Math.max(...boardColumnCounts, 0) / BOARD_PAGE_SIZE),
  );

  // Live feed (seeded once from each member's last action; not a live stream)
  type FeedItem = { id: string; time: string; text: string; agent: string };
  const feed = useMemo<FeedItem[]>(
    () =>
      members.slice(0, 4).map((m, i) => ({
        id: `seed-${m.id}-${i}`,
        time: nowTime(),
        text: m.lastAction,
        agent: m.name,
      })),
    [members],
  );

  // Real grants (from useKnowledgeBases -> linkedDepartments), not mock state.
  const deptKbs = useMemo(
    () => allBases.filter((kb) => kb.linkedDepartments.includes(id)).map((kb) => kb.id),
    [allBases, id],
  );

  if (isLoading) {
    return (
      <div className="space-y-6">
        <Link
          to="/"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-primary"
        >
          <ArrowLeft className="h-4 w-4" /> {t("Back to office", "Zurück zum Büro")}
        </Link>
        <Panel className="p-8 text-center text-sm text-muted-foreground">
          {t("Loading department…", "Abteilung wird geladen…")}
        </Panel>
      </div>
    );
  }

  if (!dept) {
    return (
      <div className="space-y-6">
        <Link
          to="/"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-primary"
        >
          <ArrowLeft className="h-4 w-4" /> {t("Back to office", "Zurück zum Büro")}
        </Link>
        <Panel className="p-8 text-center text-sm text-muted-foreground">
          {t(
            "Department not found, or you don't have access.",
            "Abteilung nicht gefunden oder kein Zugriff.",
          )}
        </Panel>
      </div>
    );
  }

  const COLUMNS: { id: TaskColumn; label: string }[] = [
    { id: "backlog", label: t("Backlog", "Backlog") },
    { id: "in_progress", label: t("In Progress", "In Bearbeitung") },
    { id: "waiting", label: t("Awaiting Approval", "Wartet auf Freigabe") },
    { id: "done", label: t("Done", "Erledigt") },
  ];
  // Backend presentation.icon is a free-form string (e.g. a freshly
  // provisioned tenant's default department uses "building"), so it can fall
  // outside the fixed icon set below — fall back rather than render undefined.
  const Icon = iconMap[dept.icon as keyof typeof iconMap] ?? Building2;
  const lead = members.find((m) => m.isLead);

  return (
    <div className="space-y-6">
      <Link
        to="/"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-primary"
      >
        <ArrowLeft className="h-4 w-4" /> {t("Back to office", "Zurück zum Büro")}
      </Link>

      {/* Header */}
      <Panel
        className="relative overflow-hidden p-6"
        // subtle accent
      >
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            background: `radial-gradient(ellipse at top left, color-mix(in oklab, ${dept.accent} 18%, transparent), transparent 60%)`,
          }}
        />
        <div className="relative flex flex-wrap items-center justify-between gap-6">
          <div className="flex items-center gap-4">
            <div
              className="grid h-14 w-14 place-items-center rounded-xl"
              style={{
                background: `color-mix(in oklab, ${dept.accent} 22%, transparent)`,
                color: dept.accent,
              }}
            >
              <Icon className="h-7 w-7" />
            </div>
            <div>
              <InlineRename
                value={dept.name}
                label={t("Rename department", "Abteilung umbenennen")}
                headingClassName="font-serif text-3xl leading-tight"
                onSave={(name) =>
                  updateDepartment.mutateAsync(
                    { name },
                    {
                      onError: () =>
                        toast.error(
                          t(
                            "Couldn't rename the department",
                            "Abteilung konnte nicht umbenannt werden",
                          ),
                        ),
                    },
                  )
                }
              />
              <p className="mt-1 text-sm text-muted-foreground">{dept.goal}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {t("Goal", "Ziel")}: {dept.okr}
              </p>
            </div>
          </div>
          {lead && (
            <div className="flex items-center gap-3 rounded-lg border border-border bg-background/40 px-3 py-2">
              <div className="relative">
                <AgentAvatar seed={lead.id} size={40} title={lead.name} />
                <Crown
                  className="absolute -top-2 -right-1 h-4 w-4 text-[color:var(--status-warning)]"
                  fill="currentColor"
                />
              </div>
              <div className="leading-tight">
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Team lead", "Teamleitung")}
                </div>
                <div className="text-sm font-medium">{lead.name}</div>
                <div className="text-xs text-muted-foreground">{lead.role}</div>
              </div>
            </div>
          )}
        </div>
        <div className="relative mt-5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground">
              {t("Efficiency / load", "Effizienz / Auslastung")}
            </span>
            <span className="font-mono">{dept.activity}%</span>
          </div>
          <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-background/60">
            <div
              className="h-full transition-[width]"
              style={{
                width: `${dept.activity}%`,
                background: `linear-gradient(90deg, ${dept.accent}, var(--primary))`,
              }}
            />
          </div>
        </div>
      </Panel>

      {/* Tab bar */}
      <div className="flex gap-1 overflow-x-auto border-b border-border">
        {[
          { id: "overview" as const, label: t("Overview", "Übersicht") },
          { id: "guardrails" as const, label: t("Guardrails", "Guardrails") },
          { id: "collab" as const, label: t("Collaboration", "Zusammenarbeit") },
          { id: "settings" as const, label: t("Settings", "Einstellungen") },
        ].map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "-mb-px border-b-2 px-4 py-2 text-sm transition",
              tab === t.id
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "guardrails" && (
        <div className="space-y-6">
          <DepartmentGuardrailsPanel departmentId={dept.id} />
        </div>
      )}

      {tab === "collab" && <CollaborationTab deptId={dept.id} />}

      {tab === "settings" && <DepartmentSettingsPanel departmentId={dept.id} />}

      {tab === "overview" && (
        <>
          <DepartmentOverviewStats departmentId={id} />

          {lead && members.length > 1 && (
            <TaskDistribution lead={lead} members={members.filter((m) => m.id !== lead.id)} />
          )}

          {/* Team roster */}
          <section>
            <div className="mb-3 flex items-center justify-between">
              <h3 className="font-serif text-lg">{t("Team roster", "Team-Übersicht")}</h3>
              <button
                type="button"
                onClick={() => setHireOpen(true)}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal"
              >
                <UserPlus className="h-4 w-4" /> {t("Hire agent", "Agent einstellen")}
              </button>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {members.map((m) => (
                <Link key={m.id} to="/agents/$id" params={{ id: m.id }} className="group">
                  <Panel className="p-4 transition hover:border-primary/40">
                    <div className="flex items-center gap-3">
                      <div className="relative">
                        <AgentAvatar seed={m.id} size={40} title={m.name} />
                        {lead?.id === m.id && (
                          <Crown
                            className="absolute -top-2 -right-1 h-3.5 w-3.5 text-[color:var(--status-warning)]"
                            fill="currentColor"
                          />
                        )}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="truncate font-medium">{m.name}</div>
                        <div className="truncate text-xs text-muted-foreground">{m.role}</div>
                      </div>
                      <StatusPill status={m.status} />
                    </div>
                    <div className="mt-3 flex items-center justify-between text-[11px] text-muted-foreground">
                      <span className="rounded border border-border bg-background/40 px-1.5 py-0.5 font-mono">
                        {m.llm}
                      </span>
                      <span>{m.lastRun}</span>
                    </div>
                    <p className="mt-2 line-clamp-2 text-xs text-muted-foreground">
                      {m.lastAction}
                    </p>
                  </Panel>
                </Link>
              ))}
            </div>
          </section>

          {/* Kanban */}
          <section>
            <div className="mb-3 flex items-center justify-between">
              <h3 className="font-serif text-lg">{t("Task board", "Aufgaben-Board")}</h3>
              <div className="flex items-center gap-3">
                <span className="text-xs text-muted-foreground">
                  {totalTasks} {t("tasks", "Aufgaben")}
                </span>
                {boardPageCount > 1 && (
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      disabled={boardPage === 0}
                      onClick={() => setBoardPage((n) => Math.max(0, n - 1))}
                      aria-label={t("Previous page", "Vorherige Seite")}
                      className="grid h-7 w-7 place-items-center rounded-md border border-border text-muted-foreground transition hover:bg-muted/40 disabled:opacity-30"
                    >
                      <ChevronLeft className="h-3.5 w-3.5" />
                    </button>
                    <span className="min-w-[64px] text-center text-xs text-muted-foreground">
                      {t(
                        `Page ${boardPage + 1} / ${boardPageCount}`,
                        `Seite ${boardPage + 1} / ${boardPageCount}`,
                      )}
                    </span>
                    <button
                      type="button"
                      disabled={boardPage >= boardPageCount - 1}
                      onClick={() => setBoardPage((n) => Math.min(boardPageCount - 1, n + 1))}
                      aria-label={t("Next page", "Nächste Seite")}
                      className="grid h-7 w-7 place-items-center rounded-md border border-border text-muted-foreground transition hover:bg-muted/40 disabled:opacity-30"
                    >
                      <ChevronRight className="h-3.5 w-3.5" />
                    </button>
                  </div>
                )}
              </div>
            </div>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              {COLUMNS.map((col) => {
                const columnItems = taskState.filter((t) => t.column === col.id);
                const items = columnItems.slice(
                  boardPage * BOARD_PAGE_SIZE,
                  boardPage * BOARD_PAGE_SIZE + BOARD_PAGE_SIZE,
                );
                const isWaiting = col.id === "waiting";
                return (
                  <div
                    key={col.id}
                    className={cn(
                      "flex min-h-[220px] flex-col rounded-lg border bg-panel/60 p-3",
                      isWaiting
                        ? "border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/5"
                        : "border-border",
                    )}
                  >
                    <div className="mb-2 flex items-center justify-between">
                      <span
                        className={cn(
                          "text-[10px] uppercase tracking-widest",
                          isWaiting
                            ? "text-[color:var(--status-warning)]"
                            : "text-muted-foreground",
                        )}
                      >
                        {col.label}
                      </span>
                      <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                        {(boardTotals[col.id] ?? columnItems.length) > columnItems.length
                          ? `${columnItems.length}/${boardTotals[col.id]}`
                          : columnItems.length}
                      </span>
                    </div>
                    <div className="flex-1 space-y-2">
                      {items.map((t) => (
                        <TaskCard key={t.id} task={t} members={members} />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          {/* Knowledge + Feed */}
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
            <Panel className="p-5">
              <div className="mb-3 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <BookOpen className="h-4 w-4" style={{ color: dept.accent }} />
                  <h3 className="font-serif text-lg">
                    {t("Department knowledge", "Abteilungswissen")}
                  </h3>
                </div>
                <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Team shared", "Team-geteilt")}
                </span>
              </div>
              <p className="mb-3 text-xs text-muted-foreground">
                {t(
                  `All agents in ${dept.name} read and write this store.`,
                  `Alle Agenten in ${dept.name} lesen und schreiben in diesen Speicher.`,
                )}{" "}
                {t(
                  'For personal notes, open an agent and switch to the "Memory" tab.',
                  'Für persönliche Notizen einen Agenten öffnen und zum Tab „Gedächtnis" wechseln.',
                )}
              </p>
              {/* No backend for department-shared notes yet — seed empty
                  rather than the old fixed mock snippets (they weren't even
                  scoped per department, so they'd misrepresent real ones). */}
              <MemoryEditor
                scope={dept.name}
                seed={[]}
                accent={dept.accent}
                emptyLabel={t(
                  `No shared knowledge in ${dept.name} yet.`,
                  `Noch kein geteiltes Wissen in ${dept.name}.`,
                )}
              />
            </Panel>

            <Panel className="flex max-h-[520px] flex-col p-0">
              <header className="flex items-center justify-between border-b border-border px-4 py-3">
                <div className="flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-primary" />
                  <h3 className="font-serif text-base">Live feed</h3>
                </div>
                <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  {t("Recent", "Zuletzt")}
                </span>
              </header>
              <ol className="flex-1 space-y-3 overflow-y-auto px-4 py-3 text-xs">
                {feed.map((f, idx) => (
                  <li
                    key={f.id}
                    className={cn(
                      "border-l border-border pl-3",
                      idx === 0 &&
                        f.id.startsWith("live-") &&
                        "animate-in fade-in slide-in-from-top-2 duration-500",
                    )}
                  >
                    <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                      <span className="font-medium text-foreground">{f.agent}</span>
                      <span>·</span>
                      <span>{f.time}</span>
                    </div>
                    <p className="mt-0.5 text-foreground/90">{f.text}</p>
                  </li>
                ))}
              </ol>
            </Panel>
          </div>

          {/* Knowledge bases assignment — enabling POSTs a real grant
              (POST /knowledge/grants) and invalidates useKnowledgeBases, so
              deptKbs above updates from the refetch. There's no delete-grant
              endpoint, so an already-granted KB renders fixed/disabled. */}
          <KnowledgeAssignment
            mode="department"
            bases={allBases}
            departmentId={dept.id}
            enabled={deptKbs}
          />

          <ComponentGrantPanel granteeType="department" granteeId={dept.id} mayManage />
        </>
      )}

      <NewAgentDialog open={hireOpen} onOpenChange={setHireOpen} defaultDepartmentId={dept.id} />
    </div>
  );
}

/** Department-scoped KPI rollup for the Overview tab (Agent KPIs & Statistics
 * plan, Task 8) -- the same three StatCards the Agent Overview tab shows
 * (Task 7's `OverviewTab`), backed by `useDepartmentKpis` instead of
 * `useAgentKpis`. Its own component, rather than inline in `DepartmentDetail`,
 * so it's reachable from a test without standing up the whole routed page's
 * board/knowledge/collaboration machinery -- same reasoning as
 * `DepartmentSettingsPanel`/`DepartmentToolsPanel` below and Task 7's
 * `OverviewTab`. */
export function DepartmentOverviewStats({ departmentId }: { departmentId: string }) {
  const t = useT();
  const kpis = useDepartmentKpis(departmentId);

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
    </div>
  );
}

/** Department-level operational settings -- currently just prompt caching,
 * but its own small independently-fetching component the way
 * DepartmentToolsPanel is, rather than folded into DepartmentDetail's
 * already-large state, so it can be tested standalone. */
export function DepartmentSettingsPanel({ departmentId }: { departmentId: string }) {
  const t = useT();
  const navigate = useNavigate();
  const { data: dept } = useDepartment(departmentId);
  const { data: members = [] } = useDepartmentAgents(departmentId);
  const updateDepartment = useUpdateDepartment(departmentId);
  const deleteDepartment = useDeleteDepartment();
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);

  if (!dept) return null;

  return (
    <div className="space-y-4">
      <Panel className="p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-medium text-foreground">
              {t("Prompt caching", "Prompt-Caching")}
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {t(
                "When two agents in this department ask the model the same thing, serve the cached answer instead of asking again.",
                "Wenn zwei Agenten in dieser Abteilung dasselbe fragen, wird die zweite Anfrage aus dem Cache beantwortet statt erneut ans Modell zu gehen.",
              )}
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={dept.promptCachingEnabled}
            onClick={() =>
              updateDepartment.mutate(
                { promptCachingEnabled: !dept.promptCachingEnabled },
                {
                  onError: () =>
                    toast.error(
                      t(
                        "Could not save prompt caching setting",
                        "Prompt-Caching-Einstellung konnte nicht gespeichert werden",
                      ),
                    ),
                },
              )
            }
            disabled={updateDepartment.isPending}
            className={cn(
              "inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition",
              dept.promptCachingEnabled
                ? "border-primary bg-primary/60"
                : "border-border bg-background/60",
            )}
          >
            <span
              className={cn(
                "h-4 w-4 rounded-full bg-foreground transition-transform",
                dept.promptCachingEnabled ? "translate-x-5" : "translate-x-1",
              )}
            />
          </button>
        </div>
      </Panel>

      <Panel className="border-destructive/30 p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-medium text-foreground">
              {t("Delete department", "Abteilung löschen")}
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {t(
                "Permanently remove this department and every agent in it.",
                "Diese Abteilung und alle Agenten darin dauerhaft entfernen.",
              )}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setDeleteModalOpen(true)}
            className="shrink-0 rounded-md border border-destructive/40 px-3 py-1.5 text-xs font-medium text-destructive transition hover:bg-destructive/10"
          >
            {t("Delete…", "Löschen…")}
          </button>
        </div>
      </Panel>

      {deleteModalOpen && (
        <DeleteDepartmentModal
          departmentName={dept.name}
          agentCount={members.length}
          isPending={deleteDepartment.isPending}
          onCancel={() => setDeleteModalOpen(false)}
          onConfirm={() =>
            deleteDepartment.mutate(departmentId, {
              onSuccess: (result) => {
                setDeleteModalOpen(false);
                toast.success(
                  result.outcome === "deleted"
                    ? t("Department deleted", "Abteilung gelöscht")
                    : t("Department archived", "Abteilung archiviert"),
                );
                navigate({ to: "/" });
              },
              onError: () =>
                toast.error(
                  t("Could not delete the department.", "Abteilung konnte nicht gelöscht werden."),
                ),
            })
          }
        />
      )}
    </div>
  );
}

/** Requires the department's own name typed exactly, matching this session's
 * standing destructive-action pattern -- a plain yes/no confirm (`useConfirm`,
 * used elsewhere for a single data source or knowledge base) isn't enough of
 * a gate for an action that also removes every agent in the department. */
function DeleteDepartmentModal({
  departmentName,
  agentCount,
  isPending,
  onCancel,
  onConfirm,
}: {
  departmentName: string;
  agentCount: number;
  isPending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useT();
  const [typed, setTyped] = useState("");
  const matches = typed === departmentName;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-xl border border-destructive/40 bg-panel p-5 shadow-xl">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <h3 className="font-serif text-lg text-foreground">
            {t("Delete", "Lösche")} "{departmentName}"?
          </h3>
        </div>
        <p className="mt-3 text-sm text-muted-foreground">
          {agentCount > 0
            ? t(
                `This also removes or archives all ${agentCount} agent${agentCount === 1 ? "" : "s"} in this department. This cannot be undone.`,
                `Dabei werden auch alle ${agentCount} Agent${agentCount === 1 ? "" : "en"} dieser Abteilung entfernt oder archiviert. Das kann nicht rückgängig gemacht werden.`,
              )
            : t("This cannot be undone.", "Das kann nicht rückgängig gemacht werden.")}
        </p>
        <label className="mt-4 block text-xs text-muted-foreground">
          {t(
            `Type "${departmentName}" to confirm`,
            `Gib "${departmentName}" ein, um zu bestätigen`,
          )}
          <input
            type="text"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoFocus
            className="mt-1 w-full rounded-md border border-border bg-background/50 px-3 py-2 text-sm text-foreground outline-none focus:border-destructive/60"
          />
        </label>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground transition hover:text-foreground"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            type="button"
            disabled={!matches || isPending}
            onClick={onConfirm}
            className="rounded-md bg-destructive px-3 py-1.5 text-sm font-medium text-destructive-foreground transition disabled:cursor-not-allowed disabled:opacity-40"
          >
            {isPending
              ? t("Deleting…", "Wird gelöscht…")
              : t("Delete department", "Abteilung löschen")}
          </button>
        </div>
      </div>
    </div>
  );
}

export function DepartmentGuardrailsPanel({ departmentId }: { departmentId: string }) {
  const t = useT();
  const { data: existing, isLoading, isError } = useDepartmentTools(departmentId);
  const { data: connections = [] } = useMcpConnections();
  const setTools = useSetDepartmentTools(departmentId);

  if (isLoading) {
    return (
      <Panel className="p-5 text-sm text-muted-foreground">
        {t("Loading guardrails…", "Guardrails werden geladen …")}
      </Panel>
    );
  }
  if (isError) {
    return (
      <Panel className="p-5 text-sm text-destructive">
        {t(
          "Could not load this department's tools, so guardrails can't be edited safely here.",
          "Die Tools dieser Abteilung konnten nicht geladen werden, daher können Guardrails hier nicht sicher bearbeitet werden.",
        )}
      </Panel>
    );
  }

  const tools = existing?.tools ?? {};
  const deviationCounts = existing?.deviationCounts ?? {};
  const agentCount = existing?.agentCount ?? 0;
  const toolKeys = Object.keys(tools);
  const connectionByName = new Map(connections.map((c) => [c.name, c] as const));
  const addableNames = connections.map((c) => c.name).filter((n) => !toolKeys.includes(n));

  function toGuardrailValue(key: string): GuardrailValue {
    const raw = (tools[key] ?? {}) as Record<string, unknown>;
    return {
      read: raw.read !== undefined ? Boolean(raw.read) : true,
      modify: Boolean(raw.modify),
      approvalActions: Array.isArray(raw.approval_actions)
        ? (raw.approval_actions as string[])
        : [],
      approvalEur: typeof raw.approval_eur === "number" ? (raw.approval_eur as number) : null,
      only: Array.isArray(raw.only) ? (raw.only as string[]) : [],
      conditions: Array.isArray(raw.conditions) ? (raw.conditions as Condition[]) : [],
    };
  }

  function persistOne(key: string, next: GuardrailValue): Promise<boolean> {
    const merged = {
      ...tools,
      [key]: {
        // Spread the tool's own previous entry first -- fields this panel
        // doesn't model (default_connection_id, the department's own default
        // login for agents with no pin of their own) must survive an edit
        // here, not silently disappear. A fixed literal deletes any field
        // it forgets, the same class of bug `toDepartmentFrame`'s own doc
        // comment used to warn about before this panel replaced it.
        ...((tools[key] ?? {}) as Record<string, unknown>),
        enabled: true,
        read: next.read,
        modify: next.modify,
        approval_eur: next.approvalEur,
        approval_actions: next.approvalActions,
        only: next.only,
        conditions: next.conditions,
      },
    };
    return new Promise((resolve) => {
      setTools.mutate(merged, {
        onSuccess: () => {
          toast.success(t("Guardrails saved", "Guardrails gespeichert"), { description: key });
          resolve(true);
        },
        onError: (error) => {
          toast.error(describeGuardrailSaveError(error, t));
          resolve(false);
        },
      });
    });
  }

  function addTool(name: string, policy: GuardrailValue | null) {
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
    );
  }

  const rows: ToolGuardrailRow[] = toolKeys
    .filter((k) => tools[k]?.enabled)
    .map((key) => ({
      toolKey: key,
      connection: connectionByName.get(key),
      ceilingPolicy: null,
      ownValue: toGuardrailValue(key),
      status: null,
      deviationCount: { count: deviationCounts[key] ?? 0, total: agentCount },
    }));

  return (
    <Panel className="p-5">
      <ConfigSectionHeader
        hint={t("tool access and guardrails", "Tool-Zugriff und Guardrails")}
        title={t("Guardrails", "Guardrails")}
      />
      {rows.length === 0 ? (
        <p className="mt-3 rounded-md border border-dashed border-border/70 bg-background/30 p-4 text-center text-xs text-muted-foreground">
          {t("No tools enabled yet.", "Noch keine Tools aktiviert.")}
        </p>
      ) : (
        <div className="mt-3">
          <ToolGuardrailTable
            level="department"
            rows={rows}
            addableNames={addableNames}
            connections={connections}
            onSave={persistOne}
            onAdd={addTool}
            saving={setTools.isPending}
          />
        </div>
      )}
    </Panel>
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

function TaskCard({ task, members }: { task: Task; members: Agent[] }) {
  const isWaiting = task.column === "waiting";
  const isDone = task.column === "done";
  return (
    <div
      className={cn(
        "group animate-in fade-in slide-in-from-top-1 rounded-md border p-2.5 text-sm transition",
        isWaiting
          ? "border-[color:var(--status-warning)]/50 bg-[color:var(--status-warning)]/10"
          : "border-border bg-background/40",
        isDone && "opacity-70",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className={cn("leading-snug", isDone && "line-through")}>{task.title}</span>
        <AgentBadge agentId={task.agentId} members={members} />
      </div>
      {task.meta && (
        <div className="mt-1 font-mono text-[10px] text-muted-foreground">{task.meta}</div>
      )}
    </div>
  );
}

function AgentBadge({ agentId, members }: { agentId: string | null; members: Agent[] }) {
  const t = useT();
  if (!agentId) {
    return (
      <span className="shrink-0 rounded-full border border-dashed border-border px-1.5 py-0.5 text-[9px] text-muted-foreground">
        {t("unassigned", "nicht zugewiesen")}
      </span>
    );
  }
  const agent = members.find((m) => m.id === agentId);
  if (!agent) return null;
  return (
    <Link
      to="/agents/$id"
      params={{ id: agent.id }}
      title={`Open ${agent.name}`}
      onClick={(e) => e.stopPropagation()}
      className="grid h-6 w-6 shrink-0 place-items-center rounded-full ring-2 ring-panel transition hover:ring-primary"
    >
      <AgentAvatar seed={agent.id} size={24} title={agent.name} />
    </Link>
  );
}

function TaskDistribution({ lead, members }: { lead: Agent; members: Agent[] }) {
  const t = useT();
  const width = 720;
  const height = 220;
  const cx = width / 2;
  const cy = height / 2;
  const radius = 88;
  const count = Math.min(members.length, 8);
  const nodes = members.slice(0, count).map((m, i) => {
    // Half-ellipse fan across the top
    const angle = Math.PI * (0.15 + (0.7 * i) / Math.max(1, count - 1));
    const x = cx + Math.cos(angle) * radius * 2.6;
    const y = cy + Math.sin(angle) * radius * 0.55 - 10;
    return { m, x, y };
  });

  return (
    <Panel className="relative overflow-hidden p-5">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" />
          <h3 className="font-serif text-lg">{t("Task distribution", "Aufgabenverteilung")}</h3>
        </div>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {t("Lead → Team", "Leitung → Team")}
        </span>
      </div>
      <div className="relative w-full">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="w-full"
          preserveAspectRatio="xMidYMid meet"
        >
          <defs>
            <radialGradient id="leadGlow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="var(--primary)" stopOpacity="0.55" />
              <stop offset="100%" stopColor="var(--primary)" stopOpacity="0" />
            </radialGradient>
          </defs>

          {/* Lead glow */}
          <circle cx={cx} cy={cy} r={54} fill="url(#leadGlow)" />

          {/* Connection lines + moving dots */}
          {nodes.map((n, i) => {
            const pathId = `dist-path-${i}`;
            const d = `M ${cx} ${cy} Q ${(cx + n.x) / 2} ${cy - 24} ${n.x} ${n.y}`;
            const dur = 2.6 + (i % 3) * 0.6;
            const delay = (i * 0.35).toFixed(2);
            return (
              <g key={n.m.id}>
                <path
                  id={pathId}
                  d={d}
                  fill="none"
                  stroke="color-mix(in oklab, var(--primary) 35%, transparent)"
                  strokeWidth={1}
                  strokeDasharray="3 4"
                />
                <circle r={3.5} fill="var(--primary)">
                  <animateMotion
                    dur={`${dur}s`}
                    repeatCount="indefinite"
                    begin={`${delay}s`}
                    rotate="auto"
                  >
                    <mpath href={`#${pathId}`} />
                  </animateMotion>
                  <animate
                    attributeName="opacity"
                    values="0;1;1;0"
                    keyTimes="0;0.15;0.85;1"
                    dur={`${dur}s`}
                    begin={`${delay}s`}
                    repeatCount="indefinite"
                  />
                </circle>
              </g>
            );
          })}

          {/* Lead node */}
          <g>
            <circle
              cx={cx}
              cy={cy}
              r={26}
              fill={lead.avatarColor}
              stroke="var(--primary)"
              strokeWidth={1.5}
            />
            <text
              x={cx}
              y={cy + 6}
              textAnchor="middle"
              fontSize="18"
              fontFamily="var(--font-serif, serif)"
              fill="#000"
            >
              {lead.name[0]}
            </text>
            <text
              x={cx}
              y={cy + 46}
              textAnchor="middle"
              fontSize="10"
              fill="var(--muted-foreground)"
              letterSpacing="2"
            >
              LEAD · {lead.name.toUpperCase()}
            </text>
          </g>

          {/* Member nodes */}
          {nodes.map((n) => (
            <g key={`node-${n.m.id}`}>
              <circle
                cx={n.x}
                cy={n.y}
                r={16}
                fill={n.m.avatarColor}
                stroke="color-mix(in oklab, var(--border) 80%, transparent)"
                strokeWidth={1}
              />
              <text
                x={n.x}
                y={n.y + 4}
                textAnchor="middle"
                fontSize="12"
                fontFamily="var(--font-serif, serif)"
                fill="#000"
              >
                {n.m.name[0]}
              </text>
              <text
                x={n.x}
                y={n.y + 32}
                textAnchor="middle"
                fontSize="10"
                fill="var(--muted-foreground)"
              >
                {n.m.name}
              </text>
              {(() => {
                const sup = supervisorOf(members, n.m);
                const supFill = sup.kind === "agent" ? sup.agent.avatarColor : "var(--primary)";
                const supLabel = sup.kind === "agent" ? sup.agent.name[0] : "H";
                return (
                  <g>
                    <title>
                      {sup.kind === "agent" ? `Supervised by ${sup.agent.name}` : "Human reviewer"}
                    </title>
                    <circle
                      cx={n.x + 12}
                      cy={n.y - 12}
                      r={7}
                      fill={supFill}
                      stroke="var(--background)"
                      strokeWidth={1.5}
                    />
                    <text
                      x={n.x + 12}
                      y={n.y - 9}
                      textAnchor="middle"
                      fontSize="8"
                      fontFamily="var(--font-serif, serif)"
                      fill="#000"
                    >
                      {supLabel}
                    </text>
                  </g>
                );
              })()}
            </g>
          ))}
        </svg>
      </div>

      <div className="mt-4 border-t border-border pt-3">
        <div className="mb-2 flex items-center justify-between">
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            {t("Reporting hierarchy", "Berichtshierarchie")}
          </div>
          <span className="text-[10px] text-muted-foreground">
            {t("agent → supervisor (thinking oversight)", "Agent → Teamleiter (Denk-Überwachung)")}
          </span>
        </div>
        <ul className="grid gap-1.5 sm:grid-cols-2">
          {members.map((m) => {
            const sup = supervisorOf(members, m);
            return (
              <li
                key={m.id}
                className="flex items-center gap-2 rounded-md border border-border bg-background/30 px-2.5 py-1.5 text-xs"
              >
                <AgentAvatar seed={m.id} size={20} title={m.name} />
                <span className="min-w-0 flex-1 truncate">
                  <span className="text-foreground">{m.name}</span>
                  <span className="text-muted-foreground"> · {m.role}</span>
                </span>
                <span className="text-muted-foreground">→</span>
                {sup.kind === "agent" ? (
                  <span className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/[0.06] px-1.5 py-0.5">
                    <AgentAvatar seed={sup.agent.id} size={16} title={sup.agent.name} />
                    <span className="text-foreground">{sup.agent.name}</span>
                    {sup.agent.isLead && (
                      <span className="rounded-full border border-primary/40 bg-primary/10 px-1 py-0 text-[9px] uppercase tracking-widest text-primary">
                        lead
                      </span>
                    )}
                  </span>
                ) : (
                  <span className="rounded-md border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-primary">
                    Human reviewer
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </Panel>
  );
}

function dtoToIntake(d: IntakeDTO): IntakeContract {
  return {
    id: d.id,
    type: d.type,
    route: d.route,
    gate: d.gate === "approval" ? "approval" : "auto",
    fields: d.fields.map((f) => ({
      name: f.name,
      type: (["string", "number", "enum", "array"].includes(f.type)
        ? f.type
        : "string") as IntakeContract["fields"][number]["type"],
      required: f.required,
      example: f.example ?? undefined,
    })),
  };
}

function intakeToDto(i: IntakeContract): IntakeDTO {
  return {
    id: i.id,
    type: i.type,
    route: i.route,
    gate: i.gate,
    fields: i.fields.map((f) => ({
      name: f.name,
      type: f.type,
      required: !!f.required,
      example: f.example ?? null,
    })),
  };
}

function CollaborationTab({ deptId }: { deptId: string }) {
  const seed = contractsFor(deptId);
  const { data: backend } = useDepartmentContracts(deptId);
  const setContracts = useSetDepartmentContracts(deptId);
  const [emits, setEmits] = useState<string[]>(seed.emits);
  const [intakes, setIntakes] = useState<IntakeContract[]>(seed.intakes);
  const [newEmit, setNewEmit] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ type: "", gate: "auto" as "auto" | "approval" });
  const [schemaFor, setSchemaFor] = useState<string | null>(null);

  // Backend is the source of truth once loaded; fall back to the seed while empty.
  useEffect(() => {
    if (!backend) return;
    if (backend.emits.length === 0 && backend.intakes.length === 0) return;
    setEmits(backend.emits);
    setIntakes(backend.intakes.map(dtoToIntake));
  }, [backend]);

  function persist(nextEmits: string[], nextIntakes: IntakeContract[]) {
    const body: DepartmentContractsDTO = {
      emits: nextEmits,
      intakes: nextIntakes.map(intakeToDto),
    };
    setContracts.mutate(body, {
      onError: () =>
        toast.error("Save failed", { description: "Contract changes were not persisted." }),
    });
  }

  function addEmit() {
    const v = newEmit.trim();
    if (!v) return;
    const next = emits.includes(v) ? emits : [...emits, v];
    setEmits(next);
    setNewEmit("");
    persist(next, intakes);
    toast.success("Event type added", { description: v });
  }
  function removeEmit(v: string) {
    const next = emits.filter((x) => x !== v);
    setEmits(next);
    persist(next, intakes);
    toast(`Removed ${v}`);
  }
  function toggleGate(id: string) {
    const next = intakes.map((i) =>
      i.id === id
        ? { ...i, gate: i.gate === "auto" ? ("approval" as const) : ("auto" as const) }
        : i,
    );
    setIntakes(next);
    persist(emits, next);
  }
  function addIntake() {
    if (!form.type.trim()) return;
    const it: IntakeContract = {
      id: `in-${Date.now()}`,
      type: form.type.trim(),
      route: "Team Lead",
      gate: form.gate,
      fields: [],
    };
    const next = [...intakes, it];
    setIntakes(next);
    setForm({ type: "", gate: "auto" });
    setShowForm(false);
    persist(emits, next);
    toast.success("Intake added", { description: it.type });
  }

  const currentSchema = intakes.find((i) => i.id === schemaFor);

  return (
    <div className="space-y-5">
      <div className="rounded-md border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-3 py-2 text-xs">
        <div className="flex items-center gap-1.5 font-medium text-[color:var(--status-warning)]">
          <AlertTriangle className="h-3.5 w-3.5" /> Contracts are governed — changes are audited and
          may suspend active flows.
        </div>
      </div>

      <Panel className="p-5">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="font-serif text-lg">Emits</h3>
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Business events published
          </span>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {emits.map((e) => (
            <span
              key={e}
              className="group inline-flex items-center gap-1 rounded-full border border-border bg-background/40 px-2 py-0.5 font-mono text-[11px]"
            >
              {e}
              <button
                onClick={() => removeEmit(e)}
                className="opacity-60 hover:text-[color:var(--status-error)]"
              >
                <XIcon className="h-3 w-3" />
              </button>
            </span>
          ))}
          {emits.length === 0 && (
            <span className="text-xs text-muted-foreground">No events yet.</span>
          )}
        </div>
        <div className="mt-3 flex gap-2">
          <input
            value={newEmit}
            onChange={(e) => setNewEmit(e.target.value)}
            placeholder="e.g. sales.deal.won"
            className="flex-1 rounded-md border border-border bg-background/40 px-2 py-1.5 font-mono text-xs outline-none focus:border-primary/50"
          />
          <button
            onClick={addEmit}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground"
          >
            <Plus className="h-3 w-3" /> Add
          </button>
        </div>
      </Panel>

      <Panel className="p-5">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="font-serif text-lg">Intakes</h3>
          <button
            onClick={() => setShowForm((s) => !s)}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-panel px-2 py-1 text-xs hover:text-primary"
          >
            <Plus className="h-3 w-3" /> Add intake
          </button>
        </div>
        {showForm && (
          <div className="mb-3 flex flex-wrap gap-2 rounded-md border border-border bg-background/40 p-3">
            <input
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}
              placeholder="Handoff type (e.g. project.kickoff)"
              className="flex-1 rounded-md border border-border bg-background/40 px-2 py-1 font-mono text-xs outline-none"
            />
            <button
              onClick={() => setForm({ ...form, gate: form.gate === "auto" ? "approval" : "auto" })}
              className={cn(
                "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]",
                form.gate === "approval"
                  ? "border-[color:var(--status-warning)]/50 text-[color:var(--status-warning)]"
                  : "border-primary/40 text-primary",
              )}
            >
              {form.gate === "approval" ? (
                <ShieldCheck className="h-3 w-3" />
              ) : (
                <Zap className="h-3 w-3" />
              )}
              {form.gate}
            </button>
            <button
              onClick={addIntake}
              className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground"
            >
              Add
            </button>
          </div>
        )}
        <div className="overflow-hidden rounded-md border border-border">
          <table className="w-full text-xs">
            <thead className="bg-background/40 text-[10px] uppercase tracking-widest text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">Handoff type</th>
                <th className="px-3 py-2 text-left">Payload schema</th>
                <th className="px-3 py-2 text-left">Route</th>
                <th className="px-3 py-2 text-left">Gate</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {intakes.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-3 py-4 text-center text-muted-foreground">
                    This department doesn't declare any intakes yet.
                  </td>
                </tr>
              )}
              {intakes.map((it) => (
                <tr key={it.id}>
                  <td className="px-3 py-2 font-mono">{it.type}</td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => setSchemaFor(it.id)}
                      className="text-primary underline-offset-2 hover:underline"
                    >
                      View schema
                    </button>
                  </td>
                  <td className="px-3 py-2">
                    <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-[10px]">
                      {it.route}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => toggleGate(it.id)}
                      className={cn(
                        "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] transition",
                        it.gate === "approval"
                          ? "border-[color:var(--status-warning)]/50 bg-[color:var(--status-warning)]/10 text-[color:var(--status-warning)]"
                          : "border-primary/40 bg-primary/10 text-primary",
                      )}
                    >
                      {it.gate === "approval" ? (
                        <ShieldCheck className="h-3 w-3" />
                      ) : (
                        <Zap className="h-3 w-3" />
                      )}
                      {it.gate}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {currentSchema && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"
          onClick={() => setSchemaFor(null)}
        >
          <div
            className="max-w-lg w-full rounded-xl border border-border bg-panel p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <h4 className="font-serif text-base">Schema · {currentSchema.type}</h4>
              <button onClick={() => setSchemaFor(null)}>
                <XIcon className="h-4 w-4 text-muted-foreground" />
              </button>
            </div>
            <pre className="overflow-x-auto rounded-md bg-background/60 p-3 font-mono text-[11px] leading-relaxed">
              {JSON.stringify(
                {
                  $schema: "https://json-schema.org/draft/2020-12/schema",
                  title: currentSchema.type,
                  type: "object",
                  required: currentSchema.fields.filter((f) => f.required).map((f) => f.name),
                  properties: Object.fromEntries(
                    currentSchema.fields.map((f) => [
                      f.name,
                      { type: f.type === "array" ? "array" : f.type, example: f.example },
                    ]),
                  ),
                },
                null,
                2,
              )}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
