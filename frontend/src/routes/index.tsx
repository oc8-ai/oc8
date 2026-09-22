import { createFileRoute, Link, Navigate, useNavigate } from "@tanstack/react-router";
import {
  Bell,
  Building2,
  Code2,
  Coins,
  Crown,
  Gauge,
  Headphones,
  Megaphone,
  TrendingUp,
  UserRound,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { AgentAvatar } from "@/components/agent-avatar";
import { Panel } from "@/components/app-shell";
import type { Agent, Department } from "@/lib/mock-data";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import {
  useAgents,
  useDepartments,
  useApprovals,
  useAuth,
  useOrganizationSettings,
} from "@/lib/hooks";
import { useCan, useMay } from "@/lib/governance-hooks";

export const Route = createFileRoute("/")({
  component: OfficePage,
});

const iconMap = {
  sales: TrendingUp,
  engineering: Code2,
  marketing: Megaphone,
  finance: Coins,
  hr: UserRound,
  support: Headphones,
} as const;

// Pure grouping helpers over a fetched `agents` list — mirrors the exact
// predicates of the former mock-data.ts helpers (agentsInDepartment,
// leadOfDepartment, isTeamLead), now parameterized on real query data
// instead of the static mock array.
const agentsInDepartment = (agents: Agent[], deptId: string) =>
  agents.filter((a) => a.departmentId === deptId);

const leadOfDepartment = (agents: Agent[], deptId: string) =>
  agents.find((a) => a.departmentId === deptId && a.isLead);

const isTeamLead = (agents: Agent[], id: string) =>
  agents.some((a) => a.id !== id && a.supervisorId === id);

function useSimClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const iv = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(iv);
  }, []);
  return {
    ticks: now.getTime() / 1000,
    label: `${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })} · ${now.toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short", year: "numeric" })}`,
  };
}

function OfficePage() {
  const { ticks, label } = useSimClock();
  const t = useT();
  const can = useCan();
  // Dashboard aggregation, not the paginated Agents/Departments list pages --
  // needs every row to count correctly, so a generous pageSize stands in
  // until the real toolbar work (Tasks 16-21) exists on THIS screen too.
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const agents = agentsPage?.items ?? [];
  const departments = departmentsPage?.items ?? [];
  const { data: pendingApprovalsList = [] } = useApprovals("pending");
  const totalAgents = departments.reduce((s, d) => s + agentsInDepartment(agents, d.id).length, 0);
  const activeAgents = departments.reduce(
    (s, d) =>
      s +
      agentsInDepartment(agents, d.id).filter(
        (a) => a.status === "running" || a.status === "warning",
      ).length,
    0,
  );
  const pendingApprovals = pendingApprovalsList.length;
  const { data: orgSettings } = useOrganizationSettings();
  // `/` is still where every sign-in lands, and this whole page is built on two
  // reads an employee's token cannot make: `department:view` and `agent:view`.
  // Without this he arrives at a floor with no rooms on it, which reads as "your
  // company has nothing in it" rather than as "this screen is not yours". `can`
  // answers true until governance has resolved, so nobody sees this flash.
  const showsTheFloor = can("department:view");
  const { data: me } = useAuth();
  const may = useMay();
  const wantsOnboarding = me?.onboardingStatus === "pending" && may("department:manage");
  const efficiency =
    departments.length === 0
      ? 0
      : Math.min(
          99,
          Math.round(
            departments.reduce((s, d) => s + d.activity, 0) / departments.length +
              Math.sin(ticks / 6) * 2,
          ),
        );

  if (wantsOnboarding) {
    return <Navigate to="/welcome" />;
  }

  if (!showsTheFloor) {
    return (
      <Panel className="p-10 text-center">
        <h2 className="font-serif text-xl">
          {t("Your work is next door", "Deine Arbeit liegt nebenan")}
        </h2>
        <p className="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
          {t(
            "The office floor is for administrators. What is waiting on you — decisions and questions from your department's agents — is on your own page.",
            "Die Büroetage ist für Administratoren. Was auf dich wartet — Entscheidungen und Rückfragen der Agenten deiner Abteilung — findest du auf deiner eigenen Seite.",
          )}
        </p>
        <Link
          to="/workspace"
          className="mt-5 inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110"
        >
          {t("Go to my work", "Zu meiner Arbeit")}
        </Link>
      </Panel>
    );
  }

  return (
    <div className="space-y-6">
      <HUD
        clock={label}
        companyName={orgSettings?.name || t("Your company", "Dein Unternehmen")}
        efficiency={efficiency}
        activeAgents={activeAgents}
        totalAgents={totalAgents}
        totalDepartments={departments.length}
        pendingApprovals={pendingApprovals}
      />


      <div className="relative rounded-2xl border border-border bg-background/60 p-4 md:p-6 grid-bg overflow-hidden">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_top,color-mix(in_oklab,var(--primary)_10%,transparent),transparent_60%)]" />

        <div className="relative mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[color:var(--status-running)] shadow-[0_0_8px_var(--status-running)]" />
            <span className="text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
              {t("Floor 1 · Live operations", "Etage 1 · Live-Betrieb")}
            </span>
          </div>
          <span className="text-[11px] text-muted-foreground">
            {departments.length} {t("rooms", "Räume")} · {totalAgents} {t("staff", "Mitarbeiter")}
          </span>
        </div>

        <div className="relative grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {departments.map((d, i) => (
            <DepartmentRoom key={d.id} d={d} agents={agents} simTick={ticks} offset={i} />
          ))}
        </div>
      </div>
    </div>
  );
}

function HUD({
  clock,
  companyName,
  efficiency,
  activeAgents,
  totalAgents,
  totalDepartments,
  pendingApprovals,
}: {
  clock: string;
  companyName: string;
  efficiency: number;
  activeAgents: number;
  totalAgents: number;
  totalDepartments: number;
  pendingApprovals: number;
}) {
  const t = useT();
  return (
    <Panel className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4 lg:p-5">
      <div className="flex items-center gap-3">
        <div className="grid h-10 w-10 place-items-center rounded-md bg-primary/15 text-primary glow-teal">
          <Gauge className="h-5 w-5" />
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            {t("Company", "Unternehmen")}
          </div>
          <div className="font-serif text-lg leading-tight">{companyName}</div>
          <div className="font-mono text-[11px] text-muted-foreground">{clock}</div>
        </div>
      </div>

      <HudStat label={t("Efficiency", "Effizienz")} value={`${efficiency}%`}>
        <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-background/60">
          <div
            className="h-full bg-primary transition-[width] duration-700 glow-teal"
            style={{ width: `${efficiency}%` }}
          />
        </div>
      </HudStat>

      <HudStat
        label={t("Active agents", "Aktive Agenten")}
        value={`${activeAgents}/${totalAgents}`}
      >
        <div className="mt-2 flex items-center gap-1 text-[11px] text-muted-foreground">
          <Users className="h-3 w-3" />
          {totalDepartments} {t("departments", "Abteilungen")}
        </div>
      </HudStat>

      {/* Was a button opening a URL-less sheet; now it goes to the workspace, so
          the tile and the sidebar land in the same place. */}
      <Link
        to="/workspace"
        className="rounded-md text-left transition hover:bg-muted/20 focus:outline-none focus:ring-2 focus:ring-primary/40 -m-2 p-2"
      >
        <HudStat
          label={t("Open approvals", "Offene Freigaben")}
          value={String(pendingApprovals)}
          highlight={pendingApprovals > 0}
        >
          <div className="mt-2 inline-flex items-center gap-1 text-[11px] text-[color:var(--status-warning)]">
            <Bell className="h-3 w-3" /> {t("Click to review", "Zum Prüfen klicken")}
          </div>
        </HudStat>
      </Link>
    </Panel>
  );
}

function HudStat({
  label,
  value,
  children,
  highlight,
}: {
  label: string;
  value: string;
  children?: React.ReactNode;
  highlight?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      <div
        className={cn(
          "mt-1 font-serif text-3xl leading-none tabular-nums",
          highlight && "text-[color:var(--status-warning)]",
        )}
      >
        {value}
      </div>
      {children}
    </div>
  );
}

function DepartmentRoom({
  d,
  agents,
  simTick,
  offset,
}: {
  d: Department;
  agents: Agent[];
  simTick: number;
  offset: number;
}) {
  const t = useT();
  const navigate = useNavigate();
  const members = agentsInDepartment(agents, d.id);
  const lead = leadOfDepartment(agents, d.id);
  // Backend presentation.icon is a free-form string (e.g. a freshly
  // provisioned tenant's default department uses "building"), so it can fall
  // outside the fixed icon set below — fall back rather than render undefined.
  const Icon = iconMap[d.icon as keyof typeof iconMap] ?? Building2;
  const dominantStatus = useMemo(() => {
    if (members.some((m) => m.status === "error")) return "error" as const;
    if (members.some((m) => m.status === "warning")) return "warning" as const;
    if (members.some((m) => m.status === "running")) return "running" as const;
    if (members.some((m) => m.status === "waiting_for_task")) return "waiting_for_task" as const;
    if (members.every((m) => m.status === "paused")) return "paused" as const;
    return "paused" as const;
  }, [members]);
  const hasWarning = members.some((m) => m.status === "warning");
  const hasActive = members.some((m) => m.status === "running" || m.status === "warning");
  const dotColor = {
    running: "var(--status-running)",
    warning: "var(--status-warning)",
    error: "var(--status-error)",
    paused: "var(--status-paused)",
    waiting_for_task: "var(--status-waiting-for-task)",
  }[dominantStatus];
  const liveActivity = Math.max(
    5,
    Math.min(99, Math.round(d.activity + Math.sin((simTick + offset * 7) / 5) * 4)),
  );

  // A plain div, not a Link: AgentTile below renders its own Link to the
  // agent, and an <a> cannot nest inside another <a>. Clicking anywhere in
  // the card OUTSIDE an agent tile still goes to the department -- agent
  // tiles stop propagation so their own Link click doesn't also fire this.
  return (
    <div
      role="link"
      tabIndex={0}
      onClick={() => navigate({ to: "/departments/$id", params: { id: d.id } })}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          navigate({ to: "/departments/$id", params: { id: d.id } });
        }
      }}
      className={cn(
        "group relative block cursor-pointer rounded-xl border bg-panel/80 p-4 transition hover:border-primary/60",
        hasWarning
          ? "border-[color:var(--status-warning)]/40 room-pulse-amber"
          : hasActive
            ? "border-border room-pulse-teal"
            : "border-border",
      )}
    >
      {hasWarning && (
        <div
          className="pointer-events-none absolute inset-0 rounded-xl"
          style={{
            background:
              "radial-gradient(ellipse at top, color-mix(in oklab, var(--status-warning) 14%, transparent), transparent 65%)",
          }}
        />
      )}
      <div
        className="pointer-events-none absolute inset-0 rounded-xl opacity-0 transition-opacity group-hover:opacity-100"
        style={{
          background: `radial-gradient(circle at 20% 0%, color-mix(in oklab, ${d.accent} 18%, transparent), transparent 60%)`,
        }}
      />
      <header className="relative flex items-start justify-between">
        <div className="flex items-center gap-3">
          <div
            className="grid h-10 w-10 place-items-center rounded-lg"
            style={{
              background: `color-mix(in oklab, ${d.accent} 20%, transparent)`,
              color: d.accent,
            }}
          >
            <Icon className="h-5 w-5" />
          </div>
          <div>
            <div className="font-serif text-lg leading-tight">{d.name}</div>
            <div className="text-xs text-muted-foreground">{d.goal}</div>
          </div>
        </div>
        <span
          className={cn(
            "flex items-center gap-1.5 text-[10px] uppercase tracking-widest",
            hasWarning ? "text-[color:var(--status-warning)]" : "text-muted-foreground",
          )}
        >
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: dotColor, boxShadow: `0 0 8px ${dotColor}` }}
          />
          {hasWarning
            ? t("Approval", "Freigabe")
            : dominantStatus === "running"
              ? t("active", "aktiv")
              : dominantStatus === "error"
                ? t("error", "Fehler")
                : dominantStatus === "waiting_for_task"
                  ? t("waiting for task", "wartet auf Aufgabe")
                  : t("paused", "pausiert")}
        </span>
      </header>

      <div className="relative mt-4 grid grid-cols-3 gap-3">
        {members.slice(0, 6).map((m) => (
          <AgentTile key={m.id} agent={m} isLead={isTeamLead(agents, m.id)} />
        ))}
      </div>

      <footer className="relative mt-4 space-y-2">
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-muted-foreground">{t("Load", "Auslastung")}</span>
          <span className="font-mono text-foreground/80 tabular-nums">{liveActivity}%</span>
        </div>
        <div className="h-1 w-full overflow-hidden rounded-full bg-background/60">
          <div
            className="h-full transition-[width] duration-700 ease-out"
            style={{
              width: `${liveActivity}%`,
              background: `linear-gradient(90deg, ${d.accent}, var(--primary))`,
            }}
          />
        </div>
        <div className="mt-2 flex items-center justify-between text-xs">
          <span className="rounded-md border border-border bg-background/40 px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
            {d.kpiValue} · {d.kpiLabel}
          </span>
          <span className="text-primary opacity-0 transition-opacity group-hover:opacity-100">
            {t("Enter room", "Raum betreten")} →
          </span>
        </div>
      </footer>
    </div>
  );
}

function AgentTile({ agent, isLead }: { agent: Agent; isLead: boolean }) {
  const dotColor = {
    running: "var(--status-running)",
    warning: "var(--status-warning)",
    error: "var(--status-error)",
    paused: "var(--status-paused)",
    waiting_for_task: "var(--status-waiting-for-task)",
  }[agent.status];
  return (
    <Link
      to="/agents/$id"
      params={{ id: agent.id }}
      onClick={(e) => e.stopPropagation()}
      className="relative flex flex-col items-center pt-7"
    >
      <div className="relative">
        <AgentAvatar
          seed={agent.id}
          size={36}
          title={agent.name}
          className="shadow-[inset_0_0_0_1px_oklch(1_0_0/25%)] transition group-hover:brightness-100 hover:brightness-110"
        />
        {isLead && (
          <Crown
            className="absolute -top-2 -right-1.5 h-3.5 w-3.5 text-[color:var(--status-warning)]"
            fill="currentColor"
          />
        )}
        <span className="absolute -bottom-0.5 -right-0.5 flex h-2.5 w-2.5">
          {agent.status !== "paused" && agent.status !== "waiting_for_task" && (
            <span
              className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-70"
              style={{ background: dotColor }}
            />
          )}
          <span
            className="relative inline-flex h-2.5 w-2.5 rounded-full ring-2 ring-panel"
            style={{ background: dotColor }}
          />
        </span>
      </div>
      <div className="mt-1.5 truncate text-[10px] text-muted-foreground hover:text-foreground">
        {agent.name}
      </div>
    </Link>
  );
}
