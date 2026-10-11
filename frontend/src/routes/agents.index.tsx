import { createFileRoute, Link } from "@tanstack/react-router";
import { Crown, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { AgentAvatar } from "@/components/agent-avatar";
import { Panel, StatusPill } from "@/components/app-shell";
import { ListToolbar, groupItems, type ListQueryState } from "@/components/list-toolbar";
import { NewAgentDialog } from "@/components/new-agent-dialog";
import { useT } from "@/lib/i18n";
import { useAgents, useDepartments, useRestoreAgent } from "@/lib/hooks";
import type { Agent, AgentStatus } from "@/lib/mock-data";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/agents/")({
  component: AgentsPage,
});

const agentStatuses: Array<{ id: AgentStatus; label: [string, string] }> = [
  { id: "running", label: ["Running", "Läuft"] },
  { id: "warning", label: ["Waiting", "Wartet"] },
  { id: "error", label: ["Error", "Fehler"] },
  { id: "paused", label: ["Paused", "Pausiert"] },
  { id: "waiting_for_task", label: ["Waiting for task", "Wartet auf Aufgabe"] },
];

export function AgentsPage() {
  const t = useT();
  const [open, setOpen] = useState(false);

  const [queryState, setQueryState] = useState<ListQueryState>({
    search: "",
    filters: {},
    groupBy: null,
    includeArchived: false,
    page: 1,
    pageSize: 20,
  });
  const { data } = useAgents({
    search: queryState.search,
    filters: queryState.filters,
    groupBy: queryState.groupBy,
    includeArchived: queryState.includeArchived,
    page: queryState.page,
    pageSize: queryState.pageSize,
  });
  const items = data?.items ?? [];

  // Options for the department filter (and group labels) come from whatever
  // departments the tenant has, same generous single-page fetch every other
  // screen uses (see e.g. skills.tsx, knowledge.tsx).
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const departmentLabel = (id: string | null | undefined) =>
    departments.find((d) => d.id === id)?.name ?? t("No department", "Keine Abteilung");
  const statusLabel = (id: string) => {
    const s = agentStatuses.find((x) => x.id === id);
    return s ? t(...s.label) : id;
  };

  const grouped = useMemo(
    () =>
      groupItems(items, queryState.groupBy, (a) =>
        queryState.groupBy === "status" ? statusLabel(a.status) : departmentLabel(a.departmentId),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, queryState.groupBy, departments],
  );

  const isTeamLead = (id: string) => items.some((a) => a.id !== id && a.supervisorId === id);

  const restoreAgent = useRestoreAgent();
  const handleRestore = (agent: Agent) => {
    restoreAgent.mutate(agent.id, {
      onSuccess: () => {
        toast.success(t("Agent restored", "Agent wiederhergestellt"), {
          description: agent.name,
        });
      },
      onError: (err: unknown) =>
        toast.error(
          err instanceof Error
            ? err.message
            : t("Could not restore the agent.", "Agent konnte nicht wiederhergestellt werden."),
        ),
    });
  };

  return (
    <div className="space-y-6">
      <Panel className="space-y-3 p-4">
        <ListToolbar
          config={{
            searchPlaceholder: t("Search agents…", "Agenten suchen…"),
            filters: [
              {
                key: "departmentId",
                label: t("Department", "Abteilung"),
                options: departments.map((d) => ({ value: d.id, label: d.name })),
              },
              {
                key: "status",
                label: t("Status", "Status"),
                options: agentStatuses.map((s) => ({ value: s.id, label: t(...s.label) })),
              },
            ],
            groupBy: [
              { value: "departmentId", label: t("Department", "Abteilung") },
              { value: "status", label: t("Status", "Status") },
            ],
            showArchivedToggle: true,
            archivedToggleLabel: t("Show archived", "Archivierte anzeigen"),
          }}
          state={queryState}
          onStateChange={setQueryState}
          totalCount={data?.totalCount ?? 0}
        />
        <div className="flex justify-end">
          <button
            onClick={() => setOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 glow-teal"
          >
            <Plus className="h-4 w-4" /> {t("Hire agent", "Agent einstellen")}
          </button>
        </div>
      </Panel>

      <div className="space-y-6">
        {grouped.map(({ group, items: groupAgents }) => (
          <div key={group ?? "all"} className="space-y-3">
            {group ? <h3 className="text-xs uppercase text-muted-foreground">{group}</h3> : null}

            <Panel className="overflow-hidden">
              <div className="hidden overflow-x-auto md:block">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                      <th className="px-5 py-3 font-medium">{t("Agent", "Agent")}</th>
                      <th className="px-5 py-3 font-medium">{t("Status", "Status")}</th>
                      <th className="px-5 py-3 font-medium">{t("LLM", "LLM")}</th>
                      <th className="px-5 py-3 font-medium">{t("Tools", "Werkzeuge")}</th>
                      <th className="px-5 py-3 font-medium">{t("Last run", "Letzter Lauf")}</th>
                      <th className="px-5 py-3 font-medium">{t("Today", "Heute")}</th>
                      <th className="px-5 py-3 font-medium" />
                    </tr>
                  </thead>
                  <tbody>
                    {groupAgents.map((a) => {
                      const archived = queryState.includeArchived && Boolean(a.deletedAt);
                      const rowBody = (
                        <>
                          <div className="relative shrink-0">
                            <AgentAvatar
                              seed={a.id}
                              size={36}
                              background="squircle"
                              title={a.name}
                            />
                            {isTeamLead(a.id) && (
                              <Crown
                                className="absolute -top-1.5 -right-1.5 h-3.5 w-3.5 text-[color:var(--status-warning)]"
                                fill="currentColor"
                              />
                            )}
                          </div>
                          <div>
                            <div className="flex items-center gap-2">
                              <span className="font-medium group-hover:text-primary">{a.name}</span>
                              {archived && (
                                <span className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[color:var(--status-warning)]">
                                  {t("Archived", "Archiviert")}
                                </span>
                              )}
                            </div>
                            <div className="text-xs text-muted-foreground">{a.role}</div>
                          </div>
                        </>
                      );
                      return (
                        <tr
                          key={a.id}
                          className={cn(
                            "group border-b border-border/60 last:border-none transition hover:bg-primary/5",
                            archived && "opacity-60",
                          )}
                        >
                          <td className="px-5 py-4">
                            {archived ? (
                              <div className="flex items-center gap-3">{rowBody}</div>
                            ) : (
                              <Link
                                to="/agents/$id"
                                params={{ id: a.id }}
                                className="flex items-center gap-3"
                              >
                                {rowBody}
                              </Link>
                            )}
                          </td>
                          <td className="px-5 py-4">
                            <StatusPill status={a.status} />
                          </td>
                          <td className="px-5 py-4">
                            <span className="rounded border border-border bg-background/40 px-2 py-1 font-mono text-xs">
                              {a.llm}
                            </span>
                          </td>
                          <td className="px-5 py-4">
                            <div className="flex flex-wrap gap-1">
                              {a.tools.slice(0, 3).map((tool) => (
                                <span
                                  key={tool}
                                  className="rounded-full border border-border bg-background/30 px-2 py-0.5 text-[10px] text-muted-foreground"
                                >
                                  {tool}
                                </span>
                              ))}
                              {a.tools.length > 3 && (
                                <span className="text-[10px] text-muted-foreground">
                                  +{a.tools.length - 3}
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="px-5 py-4 text-xs text-muted-foreground">{a.lastRun}</td>
                          <td className="px-5 py-4 font-mono text-sm">{a.tasksToday}</td>
                          <td className="px-5 py-4 text-right">
                            {archived && (
                              <button
                                type="button"
                                onClick={() => handleRestore(a)}
                                disabled={restoreAgent.isPending}
                                className="rounded-md border border-border px-2.5 py-1 text-xs transition hover:border-primary/50 hover:text-foreground disabled:opacity-50"
                              >
                                {restoreAgent.isPending
                                  ? t("Restoring…", "Wird wiederhergestellt…")
                                  : t("Restore", "Wiederherstellen")}
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {/* Mobile cards */}
              <div className="grid gap-3 p-3 md:hidden">
                {groupAgents.map((a) => {
                  const archived = queryState.includeArchived && Boolean(a.deletedAt);
                  const cardBody = (
                    <div className="flex items-center gap-3">
                      <div className="relative shrink-0">
                        <AgentAvatar seed={a.id} size={36} background="squircle" title={a.name} />
                        {isTeamLead(a.id) && (
                          <Crown
                            className="absolute -top-1.5 -right-1.5 h-3.5 w-3.5 text-[color:var(--status-warning)]"
                            fill="currentColor"
                          />
                        )}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate font-medium">{a.name}</span>
                          {archived && (
                            <span className="shrink-0 rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[color:var(--status-warning)]">
                              {t("Archived", "Archiviert")}
                            </span>
                          )}
                        </div>
                        <div className="truncate text-xs text-muted-foreground">{a.role}</div>
                      </div>
                      <StatusPill status={a.status} />
                    </div>
                  );
                  return archived ? (
                    <div
                      key={a.id}
                      className="space-y-2 rounded-lg border border-border bg-background/40 p-3 opacity-60"
                    >
                      {cardBody}
                      <button
                        type="button"
                        onClick={() => handleRestore(a)}
                        disabled={restoreAgent.isPending}
                        className="w-full rounded-md border border-border px-2.5 py-1 text-xs transition hover:border-primary/50 hover:text-foreground disabled:opacity-50"
                      >
                        {restoreAgent.isPending
                          ? t("Restoring…", "Wird wiederhergestellt…")
                          : t("Restore", "Wiederherstellen")}
                      </button>
                    </div>
                  ) : (
                    <Link
                      key={a.id}
                      to="/agents/$id"
                      params={{ id: a.id }}
                      className="rounded-lg border border-border bg-background/40 p-3"
                    >
                      {cardBody}
                    </Link>
                  );
                })}
              </div>
            </Panel>
          </div>
        ))}
        {items.length === 0 && (
          <Panel className="p-10 text-center text-sm text-muted-foreground">
            {t("No agents match your filters.", "Keine Agenten entsprechen den Filtern.")}
          </Panel>
        )}
      </div>

      <NewAgentDialog open={open} onOpenChange={setOpen} />
    </div>
  );
}
