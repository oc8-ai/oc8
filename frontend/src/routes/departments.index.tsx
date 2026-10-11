import { createFileRoute, Link } from "@tanstack/react-router";
import {
  Building2,
  Code2,
  Coins,
  Crown,
  Headphones,
  Megaphone,
  Plus,
  TrendingUp,
  UserRound,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { AgentAvatar } from "@/components/agent-avatar";
import { Panel } from "@/components/app-shell";
import { ListToolbar, type ListQueryState } from "@/components/list-toolbar";
import { useAgents, useCreateDepartment, useDepartments, useRestoreDepartment } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import type { Department } from "@/lib/mock-data";

const iconMap = {
  sales: TrendingUp,
  engineering: Code2,
  marketing: Megaphone,
  finance: Coins,
  hr: UserRound,
  support: Headphones,
} as const;

export const Route = createFileRoute("/departments/")({
  component: DepartmentsPage,
});

export function DepartmentsPage() {
  const t = useT();

  const [queryState, setQueryState] = useState<ListQueryState>({
    search: "",
    filters: {},
    groupBy: null,
    includeArchived: false,
    page: 1,
    pageSize: 20,
  });
  // Departments has no `groupBy` -- Task 7 deliberately left `group_fields={}`
  // for it (no sensible orderable group field), so `queryState.groupBy` stays
  // null and no `groupBy` config is passed to <ListToolbar> below.
  const { data: departmentsPage } = useDepartments({
    search: queryState.search,
    includeArchived: queryState.includeArchived,
    page: queryState.page,
    pageSize: queryState.pageSize,
  });
  // Secondary aggregation (member counts per card below), not the list view
  // itself, so it keeps a generous pageSize to avoid under-counting.
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const departments = departmentsPage?.items ?? [];
  const agents = agentsPage?.items ?? [];
  const membersOf = (id: string) => agents.filter((a) => a.departmentId === id);
  const leadOf = (id: string) => agents.find((a) => a.departmentId === id && a.isLead);
  const [dialog, setDialog] = useState(false);
  const createDepartment = useCreateDepartment();

  const restoreDepartment = useRestoreDepartment();
  const handleRestore = (department: Department) => {
    restoreDepartment.mutate(department.id, {
      onSuccess: () => {
        toast.success(t("Department restored", "Abteilung wiederhergestellt"), {
          description: department.name,
        });
      },
      onError: (err: unknown) =>
        toast.error(
          err instanceof Error
            ? err.message
            : t(
                "Could not restore the department.",
                "Abteilung konnte nicht wiederhergestellt werden.",
              ),
        ),
    });
  };

  return (
    <div className="space-y-6">
      <Panel className="space-y-3 p-4">
        <ListToolbar
          config={{
            searchPlaceholder: t("Search departments…", "Abteilungen suchen…"),
            showArchivedToggle: true,
            archivedToggleLabel: t("Show archived", "Archivierte anzeigen"),
          }}
          state={queryState}
          onStateChange={setQueryState}
          totalCount={departmentsPage?.totalCount ?? 0}
        />
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">
            {departmentsPage?.totalCount ?? 0}{" "}
            {t("departments · jointly coordinated", "Abteilungen · gemeinsam koordiniert")}
          </p>
          <button
            onClick={() => setDialog(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:brightness-110 glow-teal"
          >
            <Plus className="h-4 w-4" /> {t("New department", "Neue Abteilung")}
          </button>
        </div>
      </Panel>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {departments.map((d) => {
          // Backend presentation.icon is a free-form string (e.g. a freshly
          // provisioned tenant's default department uses "building"), so it can
          // fall outside the fixed icon set below — fall back rather than render
          // undefined.
          const Icon = iconMap[d.icon as keyof typeof iconMap] ?? Building2;
          const members = membersOf(d.id);
          const lead = leadOf(d.id);
          const waiting = members.filter((m) => m.status === "warning").length;
          const archived = queryState.includeArchived && Boolean(d.deletedAt);

          const cardContent = (
            <Panel
              className={`relative overflow-hidden p-5 transition ${archived ? "opacity-60" : "hover:border-primary/50 hover:room-glow"}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-3">
                  <div
                    className="grid h-11 w-11 place-items-center rounded-lg"
                    style={{
                      background: `color-mix(in oklab, ${d.accent} 22%, transparent)`,
                      color: d.accent,
                    }}
                  >
                    <Icon className="h-5 w-5" />
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-serif text-xl leading-tight">{d.name}</span>
                      {archived && (
                        <span className="rounded-full border border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[color:var(--status-warning)]">
                          {t("Archived", "Archiviert")}
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground">{d.goal}</div>
                  </div>
                </div>
                {waiting > 0 && (
                  <span className="rounded-full bg-[color:var(--status-warning)]/15 px-2 py-0.5 text-[10px] font-medium text-[color:var(--status-warning)]">
                    {waiting}{" "}
                    {waiting > 1 ? t("approvals", "Freigaben") : t("approval", "Freigabe")}
                  </span>
                )}
              </div>

              <div className="mt-4 text-xs text-muted-foreground">{t("Goal", "Ziel")}</div>
              <p className="mt-1 text-sm">{d.okr}</p>

              <div className="mt-4 flex items-center gap-2">
                {members.map((m) => (
                  <div key={m.id} className="relative">
                    <AgentAvatar
                      seed={m.id}
                      size={28}
                      className="ring-2 ring-panel"
                      title={m.name}
                    />
                    {lead?.id === m.id && (
                      <Crown className="absolute -top-1.5 -right-1 h-3 w-3 text-[color:var(--status-warning)]" fill="currentColor" />
                    )}
                  </div>
                ))}
              </div>

              <div className="mt-4 flex items-center justify-between text-xs">
                <span className="text-muted-foreground">{t("Load", "Auslastung")}</span>
                <span className="font-mono">{d.activity}%</span>
              </div>
              <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-background/60">
                <div
                  className="h-full"
                  style={{
                    width: `${d.activity}%`,
                    background: `linear-gradient(90deg, ${d.accent}, var(--primary))`,
                  }}
                />
              </div>
              <div className="mt-3 flex items-center justify-between text-[11px]">
                <span className="rounded-md border border-border bg-background/40 px-2 py-0.5 font-mono text-muted-foreground">
                  {d.kpiValue} · {d.kpiLabel}
                </span>
                {archived ? (
                  <button
                    type="button"
                    onClick={() => handleRestore(d)}
                    disabled={restoreDepartment.isPending}
                    className="rounded-md border border-border px-2.5 py-1 text-xs transition hover:border-primary/50 hover:text-foreground disabled:opacity-50"
                  >
                    {restoreDepartment.isPending
                      ? t("Restoring…", "Wird wiederhergestellt…")
                      : t("Restore", "Wiederherstellen")}
                  </button>
                ) : (
                  <span className="text-primary opacity-0 transition-opacity group-hover:opacity-100">
                    {t("Open", "Öffnen")} →
                  </span>
                )}
              </div>
            </Panel>
          );

          return archived ? (
            <div key={d.id} className="group block">
              {cardContent}
            </div>
          ) : (
            <Link
              key={d.id}
              to="/departments/$id"
              params={{ id: d.id }}
              className="group block"
            >
              {cardContent}
            </Link>
          );
        })}
      </div>
      {departments.length === 0 && (
        <Panel className="p-10 text-center text-sm text-muted-foreground">
          {t("No departments match your filters.", "Keine Abteilungen entsprechen den Filtern.")}
        </Panel>
      )}

      {dialog && (
        <NewDepartmentDialog
          onClose={() => setDialog(false)}
          onCreate={(name, goal) => {
            createDepartment.mutate(
              { name, goal },
              {
                onSuccess: () => {
                  toast.success(t(`Department "${name}" created`, `Abteilung „${name}" erstellt`), {
                    description: t(
                      "Hire an agent into it from the Agents page.",
                      "Stellen Sie einen Agenten dafür auf der Agenten-Seite ein.",
                    ),
                  });
                  setDialog(false);
                },
                onError: (err) =>
                  toast.error(t("Could not create department", "Abteilung konnte nicht angelegt werden"), {
                    description: err instanceof Error ? err.message : String(err),
                  }),
              },
            );
          }}
        />
      )}
    </div>
  );
}

function NewDepartmentDialog({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (name: string, goal: string) => void;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  return (
    <div
      className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <Panel
        className="w-full max-w-md p-6"
        // stopPropagation handled by inner div
      >
        <div onClick={(e) => e.stopPropagation()}>
          <h2 className="font-serif text-2xl">{t("New department", "Neue Abteilung")}</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {t(
              "Create a room for a new team. You can assign agents later.",
              "Legen Sie einen Raum für ein neues Team an. Agenten können später zugewiesen werden.",
            )}
          </p>
          <div className="mt-4 space-y-3">
            <label className="block">
              <span className="text-xs text-muted-foreground">{t("Name", "Name")}</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                placeholder={t("e.g. Legal & Compliance", "z. B. Recht & Compliance")}
                className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
              />
            </label>
            <label className="block">
              <span className="text-xs text-muted-foreground">{t("Goal / OKR", "Ziel / OKR")}</span>
              <input
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                placeholder={t("e.g. Review contracts on time", "z. B. Verträge fristgerecht prüfen")}
                className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
              />
            </label>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <button
              onClick={onClose}
              className="rounded-md border border-border bg-panel px-3 py-2 text-sm text-foreground/80 hover:text-foreground"
            >
              {t("Cancel", "Abbrechen")}
            </button>
            <button
              disabled={!name}
              onClick={() => onCreate(name, goal)}
              className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40 hover:brightness-110 glow-teal"
            >
              {t("Create", "Erstellen")}
            </button>
          </div>
        </div>
      </Panel>
    </div>
  );
}