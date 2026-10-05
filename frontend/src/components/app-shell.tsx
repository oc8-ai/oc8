import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import {
  Activity,
  BarChart3,
  Bell,
  Building2,
  Check,
  ChevronDown,
  ChevronRight,
  Coins,
  Cpu,
  Database,
  GitBranch,
  Handshake,
  Inbox,
  KeyRound,
  LayoutGrid,
  LogOut,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Puzzle,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Sun,
  TrendingUp,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { NotificationsSheet, OPEN_NOTIFICATIONS_EVENT } from "@/components/inbox-sheets";
import { GlobalSearch } from "@/components/global-search";
import { CopilotDock } from "@/components/copilot-dock";
import { CopilotAvatar } from "@/components/copilot-persona";
import { useCopilotProfile } from "@/lib/hooks-copilot";
import { mascotState } from "@/lib/copilot-status";
import { cn } from "@/lib/utils";
import { useAgents, useApprovals, useClarifications, useStanding } from "@/lib/hooks";
import { useAssignedRoleName, useCan, useMay } from "@/lib/governance-hooks";
import { useLang, useT } from "@/lib/i18n";
import { hasCommunitySession, logoutCommunity, logoutDev } from "@/lib/api";
import {
  getPushSubscriptionStatus,
  listenForPushSubscriptionChanges,
  unsubscribeFromPush,
} from "@/lib/push-notifications";
import { useFrontendEdition } from "@/edition/composition";

/** Hand this device's push subscription back before the session ends.
 *
 *  A `pushManager` subscription belongs to the DEVICE, not to whoever happens
 *  to be signed in -- on a shared browser the next person's sign-in produces
 *  the very same subscription. Left behind, it keeps delivering the previous
 *  operator's approval titles to a machine they may have walked away from.
 *
 *  Never allowed to fail loudly, and awaited only via `.finally()` at the call
 *  site: an offline device, a stopped server, or a browser that refuses must
 *  not be able to trap someone in a session they asked to leave. */
async function revokePushOnSignOut(): Promise<void> {
  try {
    const subscription = await getPushSubscriptionStatus();
    if (subscription) await unsubscribeFromPush(subscription);
  } catch {
    // Signing out wins -- see above.
  }
}

/** Up to two letters from whatever name we actually have. "?" rather than a
 *  plausible-looking placeholder: a wrong pair of initials in the corner of a
 *  screen that decides money is worse than an obvious blank. */
function initials(name: string): string {
  const parts = name
    .trim()
    .split(/[\s._@-]+/)
    .filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** The caller's role, in words. Unknown roles are shown VERBATIM rather than
 *  normalised to "Member": "your token says `menber`" is the answer somebody can
 *  act on, and it is the same rule the governance screen already follows. */
export function roleLabel(role: string, t: (en: string, de: string) => string): string {
  switch (role) {
    case "org_admin":
      return t("Admin", "Administrator");
    case "operator":
      return t("Operator", "Operator");
    case "auditor":
      return t("Auditor", "Prüfer");
    case "dept_manager":
      return t("Department lead", "Abteilungsleitung");
    case "member":
      return t("Employee", "Mitarbeiter");
    case "":
      return "";
    default:
      return role;
  }
}

/** One navigable screen. */
interface NavLink {
  to: string;
  label: string;
  icon: typeof Building2;
  /** Optional element drawn right after the label (the Copilot's avatar). */
  adornment?: ReactNode;
  exact?: boolean;
  /** The tenant-wide permission the screen's data sits behind. A screen he may
   *  not open is a screen he is not shown — an employee's role is `member`,
   *  which holds the empty set, so everything with a `needs` disappears from
   *  his sidebar and Workspace and Access are what remain. Every built-in role
   *  above `member` holds every `:view` in the system except secret and audit,
   *  so nobody who can see an item today loses it. */
  needs?: string;
  /** Answer `needs` STRICTLY: hide while the answer is unknown, rather than
   *  showing and then disappearing.
   *
   *  Both behaviours are right, for different items, and the difference is what
   *  the item was before. `useCan()` answers TRUE while `/governance` is in
   *  flight, because an empty sidebar on every administrator's first paint is a
   *  worse lie than a link that 403s once. But `/capas` (then still `/plugins`)
   *  and `/audit` were `adminOnly`, and `useIsAdmin()` — whose own comment read
   *  "false while pending/errored → fail-safe hide" — returned FALSE in exactly
   *  that window. Converting them to `needs` without this flag would have made
   *  an ordinary employee see Capas and Prüfprotokoll on every cold load until
   *  the query landed: a regression against the contract those two call sites
   *  were written to, and against no other item's. */
  strict?: boolean;
  badge?: number;
}

/** A group of screens with a shared question. A section is NOT a route: it has
 *  no `to`, so it can never become an "active" item competing with the child
 *  the reader is actually on, and it can never be a destination that then has to
 *  decide which of its children to redirect to. */
interface NavSection {
  section: string;
  label: string;
  icon: typeof Building2;
  children: NavLink[];
}

type NavEntry = NavLink | NavSection;

function isSection(entry: NavEntry): entry is NavSection {
  return "section" in entry;
}

const SECTION_STATE_KEY = "oc8-nav-sections";

/** Which sections the reader has collapsed, remembered across navigations and
 *  reloads. Stored as the set of CLOSED ids rather than open ones, so a section
 *  added in a later release starts open rather than starting invisible to
 *  everybody who has ever used the product. */
function useCollapsedSections(): [Set<string>, (id: string) => void, (id: string) => void] {
  const [closed, setClosed] = useState<Set<string>>(new Set());

  // After mount, not during render: reading localStorage while rendering makes
  // the server and the client disagree about the first paint.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(SECTION_STATE_KEY);
      if (raw) setClosed(new Set(JSON.parse(raw) as string[]));
    } catch {
      /* a corrupt value is not worth a broken sidebar */
    }
  }, []);

  const persist = (next: Set<string>) => {
    try {
      window.localStorage.setItem(SECTION_STATE_KEY, JSON.stringify([...next]));
    } catch {
      /* noop */
    }
    return next;
  };

  const toggle = useCallback((id: string) => {
    setClosed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return persist(next);
    });
  }, []);

  // Opening a section the reader has just navigated INTO, which is a different
  // act from the reader opening it: it happens once, on arrival, and leaves the
  // section openable and closable afterwards.
  //
  // The alternative was `open = holdsActive || !collapsed`, and it was wrong in
  // a way that only shows up in use. It made the chevron inert for exactly the
  // reader most likely to press it -- somebody standing on /models, looking at
  // four siblings he does not want -- and it made the "you are in a collapsed
  // section" dot unreachable, because a section holding the active child could
  // never be collapsed to show it. Two documented rules, one of which silently
  // could not happen.
  //
  // Returns the SAME set when there is nothing to open, so React bails out
  // rather than re-rendering the sidebar on every pass.
  const reveal = useCallback((id: string) => {
    setClosed((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return persist(next);
    });
  }, []);

  return [closed, toggle, reveal];
}

const SIDEBAR_COLLAPSED_KEY = "oc8-sidebar-collapsed";

/** The reader's own persisted choice for every page EXCEPT /workspace, which always
 *  forces the rail regardless of this value (see AppShell's `collapsed`). */
function usePersistedSidebarCollapsed(): [boolean, (next: boolean) => void] {
  const [collapsed, setCollapsedState] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
  });
  const setCollapsed = useCallback((next: boolean) => {
    setCollapsedState(next);
    try {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
    } catch {
      /* noop, matches the sections-state precedent */
    }
  }, []);
  return [collapsed, setCollapsed];
}

function NavItemLink({
  item,
  pathname,
  nested = false,
  collapsed = false,
}: {
  item: NavLink;
  pathname: string;
  nested?: boolean;
  /** Whole-sidebar rail state (not this item's own state) -- hides the label
   *  and swaps the numeric badge pill for a plain dot, since neither fits an
   *  icon-only column. */
  collapsed?: boolean;
}) {
  const active = item.exact ? pathname === item.to : pathname.startsWith(item.to);
  const Icon = item.icon;
  const hasBadge = item.badge != null && item.badge > 0;
  return (
    <Link
      to={item.to as "/"}
      title={collapsed ? item.label : undefined}
      className={cn(
        "group flex items-center gap-3 rounded-md py-2 text-sm transition-colors",
        // Nested items are indented by the rule to their left rather than by
        // padding alone, so the label keeps its width and still truncates
        // cleanly when the sidebar is the narrowest it ever gets.
        collapsed ? "justify-center px-2" : nested ? "pl-3 pr-3" : "px-3",
        active
          ? "bg-primary/10 text-primary"
          : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-foreground",
      )}
      aria-current={active ? "page" : undefined}
    >
      <span className="relative inline-flex shrink-0">
        <Icon
          className={cn(
            "h-4 w-4 shrink-0",
            active ? "text-primary" : "text-muted-foreground group-hover:text-foreground",
          )}
        />
        {collapsed && hasBadge && (
          <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-[color:var(--status-warning)]" />
        )}
      </span>
      {!collapsed && (
        <>
          <span className="truncate">{item.label}</span>
          {item.adornment}
          {hasBadge && (
            <span className="ml-auto grid h-5 min-w-5 place-items-center rounded-full bg-[color:var(--status-warning)] px-1 text-[10px] font-semibold text-black">
              {item.badge}
            </span>
          )}
          {active && (
            <span
              className={cn(
                "h-1.5 w-1.5 shrink-0 rounded-full bg-primary shadow-[0_0_10px_var(--primary)]",
                item.badge ? "ml-1.5" : "ml-auto",
              )}
            />
          )}
        </>
      )}
    </Link>
  );
}

/** A collapsible group.
 *
 * Two rules the reader never has to think about, and they have to be two
 * separate mechanisms rather than one expression. **Navigating into a collapsed
 * section opens it** — arriving somewhere must not leave the sidebar looking
 * like you are nowhere. **Collapsing it while you are standing in it still
 * works**, and then the heading wears the same dot an active item wears, so the
 * sidebar still says where you are. The first is an effect that fires on
 * arrival; the second is the reader's stored choice, which nothing overrides.
 */
function NavItemSection({
  section,
  items,
  pathname,
  collapsed,
  onToggle,
  onReveal,
  sidebarCollapsed = false,
  onExpandSidebar,
}: {
  section: NavSection;
  items: NavLink[];
  pathname: string;
  collapsed: boolean;
  onToggle: () => void;
  onReveal: (id: string) => void;
  /** Whole-sidebar rail state. An icon column has no room for this section's
   *  own chevron/children, so while this is true the section renders as a
   *  single icon-only item instead (see the early return below). */
  sidebarCollapsed?: boolean;
  /** Un-collapses the whole sidebar -- the same setter the dock's toggle
   *  button uses. Only called from the rail-mode click handler below. */
  onExpandSidebar?: () => void;
}) {
  const t = useT();
  const holdsActive = items.some((c) => (c.exact ? pathname === c.to : pathname.startsWith(c.to)));
  const id = section.section;
  useEffect(() => {
    if (holdsActive) onReveal(id);
  }, [holdsActive, id, onReveal]);
  const open = !collapsed;
  const Icon = section.icon;
  const panelId = `nav-section-${section.section}`;

  // The rail has no room for a chevron or its children -- clicking the
  // section here does two things at once: it un-collapses the whole sidebar
  // AND reveals this section, so the reader lands exactly where they clicked
  // rather than on a still-closed section in a now-wide sidebar.
  if (sidebarCollapsed) {
    return (
      <button
        type="button"
        onClick={() => {
          onExpandSidebar?.();
          onReveal(id);
        }}
        title={section.label}
        className={cn(
          "group flex w-full items-center justify-center rounded-md py-2 text-sm transition-colors",
          holdsActive
            ? "text-sidebar-foreground"
            : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-foreground",
        )}
      >
        <span className="relative inline-flex shrink-0">
          <Icon
            className={cn(
              "h-4 w-4 shrink-0",
              holdsActive ? "text-primary" : "text-muted-foreground group-hover:text-foreground",
            )}
          />
          {holdsActive && (
            <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-primary shadow-[0_0_10px_var(--primary)]" />
          )}
        </span>
      </button>
    );
  }

  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={panelId}
        title={
          open
            ? t(`Collapse ${section.label}`, `${section.label} einklappen`)
            : t(`Expand ${section.label}`, `${section.label} ausklappen`)
        }
        className={cn(
          "group flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
          holdsActive
            ? "text-sidebar-foreground"
            : "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-foreground",
        )}
      >
        <Icon
          className={cn(
            "h-4 w-4 shrink-0",
            holdsActive ? "text-primary" : "text-muted-foreground group-hover:text-foreground",
          )}
        />
        <span className="truncate">{section.label}</span>
        {holdsActive && !open && (
          <span className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-primary shadow-[0_0_10px_var(--primary)]" />
        )}
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform",
            holdsActive && !open ? "ml-1.5" : "ml-auto",
            open && "rotate-90",
          )}
        />
      </button>
      {open && (
        <div
          id={panelId}
          className="mt-1 space-y-1 border-l border-sidebar-border pl-3 ml-[1.4rem]"
        >
          {items.map((child) => (
            <NavItemLink key={child.to} item={child} pathname={pathname} nested />
          ))}
        </div>
      )}
    </div>
  );
}

export function AppShell() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const navigate = useNavigate();
  const t = useT();
  const { lang, setLang, locales } = useLang();
  const can = useCan();
  const may = useMay();
  const standing = useStanding();
  // The role as it RESOLVES, not as the token spells it. See
  // `useAssignedRoleName`: this corner said "Administrator" to a demoted
  // administrator on every page.
  const assignedRole = useAssignedRoleName();
  const [closedSections, toggleSection, revealSection] = useCollapsedSections();

  // Two mechanisms, deliberately kept apart -- see `usePersistedSidebarCollapsed`'s
  // doc comment and the design note in the sidebar-collapse spec. `userCollapsed`
  // is the reader's own persisted choice, honoured on every route except
  // /workspace. `workspaceExpandOverride` is a same-visit-only override that
  // reverts the moment the reader arrives at /workspace fresh -- it is never
  // written to storage, so it can never leak into `userCollapsed`'s behaviour
  // elsewhere, and clicking it while on /workspace never touches the persisted key.
  const [userCollapsed, setUserCollapsed] = usePersistedSidebarCollapsed();
  const onWorkspace = pathname.startsWith("/workspace");
  const [workspaceExpandOverride, setWorkspaceExpandOverride] = useState(false);
  useEffect(() => {
    if (onWorkspace) setWorkspaceExpandOverride(false);
  }, [onWorkspace]);
  const collapsed = onWorkspace ? !workspaceExpandOverride : userCollapsed;
  const toggleSidebar = () => {
    if (onWorkspace) setWorkspaceExpandOverride((v) => !v);
    else setUserCollapsed(!userCollapsed);
  };
  // Passed to a collapsed section's rail-mode click handler -- un-collapsing
  // is a one-way expand there, never a toggle (see `NavItemSection`).
  const expandSidebar = useCallback(() => {
    if (onWorkspace) setWorkspaceExpandOverride(true);
    else setUserCollapsed(false);
  }, [onWorkspace, setUserCollapsed]);

  const { data: approvals = [] } = useApprovals("pending");
  const { data: clarifications = [] } = useClarifications();
  // Sidebar notification badge over every agent, not a paginated list view.
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const agents = agentsPage?.items ?? [];
  // One badge over ONE queue: the workspace holds both kinds of waiting work, so
  // a count that only knew about approvals would send somebody past a question
  // an agent has been parked on for two hours.
  const workCount = approvals.length + clarifications.length;
  const { data: copilotProfile } = useCopilotProfile(can("copilot:use"));
  const copilotAdornment = (
    <span className="inline-flex items-center gap-1" data-testid="nav-copilot-adornment">
      <CopilotAvatar
        avatar={copilotProfile?.avatar ?? { shape: "round", color: "indigo" }}
        state={mascotState(copilotProfile?.status ?? "ready")}
        size={16}
      />
      {copilotProfile?.status === "working" && (
        <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--status-running)]" />
      )}
      {copilotProfile?.status === "waiting" && (
        <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--status-warning)]" />
      )}
    </span>
  );
  const notifCount = agents.filter((a) => a.status === "warning" || a.status === "error").length;
  const edition = useFrontendEdition();

  const nav: NavEntry[] = [
    { to: "/", label: t("Office", "Büro"), icon: Building2, exact: true, needs: "department:view" },
    // Second, directly under the Office, and deliberately NOT gated on `needs`:
    // its authority is a SEAT, which `callerPermissions` does not and must not
    // describe. Hiding it behind a tenant-wide permission would hide it from
    // exactly the employee it exists for. It also stays TOP-LEVEL rather than
    // moving under Settings: it is where an employee spends the day, not a piece
    // of administration.
    {
      to: "/workspace",
      label: t("My work", "Meine Arbeit"),
      icon: Inbox,
      badge: workCount,
    },
    {
      to: "/copilot",
      label: t("Copilot", "Copilot"),
      icon: Sparkles,
      needs: "copilot:use",
      adornment: copilotAdornment,
    },
    {
      to: "/departments",
      label: t("Departments", "Abteilungen"),
      icon: LayoutGrid,
      needs: "department:view",
    },
    { to: "/agents", label: t("Agents", "Agenten"), icon: Users, needs: "agent:view" },
    { to: "/skills", label: t("Skills", "Skills"), icon: Sparkles, needs: "skill:view" },
    { to: "/handoffs", label: t("Handoffs", "Übergaben"), icon: Handshake, needs: "handoff:view" },
    { to: "/flows", label: t("Flows", "Abläufe"), icon: GitBranch, needs: "flow:view" },
    {
      // Merges the old App Store (100% mock), Integrations (real MCP
      // connections plus a dead mock catalog) and Plugins (the only real
      // install/enable lifecycle of the three) into one concept. Gated on
      // the lowest of the three original bars -- integration:view -- so
      // connection management stays as broadly reachable as it always was;
      // the page itself hides installed/store management behind an inline
      // plugin:manage check, matching Plugins' own deliberate scoping.
      to: "/capas",
      label: t("Capas", "Capas"),
      icon: Puzzle,
      needs: "integration:view",
    },
    { to: "/knowledge", label: t("Knowledge", "Wissen"), icon: Database, needs: "knowledge:view" },
    {
      // Three screens you go to in order to WATCH work, not to change how the
      // system behaves -- that's the same rule Settings below applies, just
      // for reporting instead of configuration. Grouped rather than left flat
      // now that there are three of them. Money is watched, not configured:
      // `budget:view` is one of the twenty-one rights a tenant-defined role
      // may actually hold, so Costs is a report an employee can be given, not
      // an admin-only screen. `statistics:view` is its own resource (backend
      // authz/permissions.py) because GET /kpis is the one read that can span
      // every agent and department in the tenant at once.
      section: "analytics",
      label: t("Analytics", "Analyse"),
      icon: TrendingUp,
      children: [
        { to: "/activity", label: t("Activity", "Aktivität"), icon: Activity, needs: "run:view" },
        { to: "/costs", label: t("Costs", "Kosten"), icon: Coins, needs: "budget:view" },
        {
          to: "/statistics",
          label: t("Statistics", "Statistiken"),
          icon: BarChart3,
          needs: "statistics:view",
          strict: true,
        },
      ],
    },
    {
      // Everything under here answers "how does this installation work", which
      // is a different question from "what is happening in it" (Analytics,
      // above) or "what work is there to do" (the flat items above that). The
      // rule this section applies is that a screen you go to in order to
      // CHANGE how the system behaves lives inside it, and a screen you go to
      // in order to do or watch work stays above.
      section: "settings",
      label: t("Settings", "Einstellungen"),
      icon: Settings,
      children: [
        // First, and named for the question rather than for the mechanism. An
        // admin looking for "who may do what" does not know the word
        // "governance" and should not have to: this is the item he is looking
        // for, and the role editor is the panel he lands on.
        //
        // Deliberately no `needs`, exactly as when it was top-level: the screen
        // exists to explain a refusal, and the caller most in need of it is the
        // one whose role grants nothing. Because it carries no gate, the section
        // itself is visible to everybody — which is the point. The tenant's
        // authority MAP inside it is behind `role:view`; the caller's own
        // explanation is not.
        {
          to: "/governance",
          label: t("Access & roles", "Zugriff & Rollen"),
          icon: KeyRound,
        },
        // Manage user roles for the organization.
        {
          to: "/members",
          label: t("Users", "Benutzer"),
          icon: Users,
          needs: "member:manage",
        },
        // Declares provider endpoints that `pdp.py` believes about locality.
        { to: "/models", label: t("Models", "Modelle"), icon: Cpu, needs: "model:view" },
        // Reusable connections to other services (§12.3 secret store metadata).
        // Gated on the same `secret:view` the backend endpoints require.
        {
          to: "/credentials",
          label: t("Credentials", "Zugangsdaten"),
          icon: KeyRound,
          needs: "secret:view",
        },
        // The compliance archive, read rarely and by few. `/activity` is the
        // operational feed and stays above; this is the tamper-evident record.
        {
          to: "/audit",
          label: t("Audit", "Prüfprotokoll"),
          icon: ShieldCheck,
          needs: "audit:view",
          strict: true,
        },
        {
          to: "/settings",
          label: t("General", "Allgemein"),
          icon: SlidersHorizontal,
          needs: "settings:view",
        },
      ],
    },
    ...edition.navigation.map(
      (item): NavLink => ({
        to: item.to,
        label: t(item.label.en, item.label.de),
        icon: item.icon,
        needs: item.needs,
        strict: item.strict,
      }),
    ),
  ];

  // `/agents` and `/departments` graduate READ for any live seat — no
  // tenant-wide permission required (department-scoped-agent-authority
  // design, decision 2). `callerPermissions`/`useCan` describe tenant-wide
  // authority only and never consult a seat, so without this a seated-only
  // member's sidebar would hide both links even though the backend now
  // admits her to both. `standing.seats` is the caller's own live seats
  // (`/me`), independent of `callerPermissions` for the same reason a seat is
  // never folded into it there.
  const SEAT_VISIBLE_NAV = new Set(["/agents", "/departments"]);
  const seated = standing.seats.length > 0;

  /** One rule for both levels: a screen the caller's role cannot open is a
   *  screen the caller is not shown. Applied to a section's children before the
   *  section itself, so a section only appears when something inside it does. */
  const visible = (item: NavLink) =>
    !item.needs ||
    (item.strict ? may(item.needs) : can(item.needs)) ||
    (SEAT_VISIBLE_NAV.has(item.to) && seated);

  const pageTitle = (p: string) => {
    if (p === "/") return t("Office", "Büro");
    if (p.startsWith("/workspace")) return t("My work", "Meine Arbeit");
    if (p.startsWith("/copilot")) return t("My Copilot", "Mein Copilot");
    if (p.startsWith("/departments")) return t("Departments", "Abteilungen");
    if (p.startsWith("/agents")) return t("Agents", "Agenten");
    if (p.startsWith("/skills")) return t("Skills", "Skills");
    if (p.startsWith("/handoffs")) return t("Handoffs", "Übergaben");
    if (p.startsWith("/flows")) return t("Flows", "Abläufe");
    if (p.startsWith("/capas")) return t("Capas", "Capas");
    if (p.startsWith("/knowledge")) return t("Knowledge", "Wissen");
    if (p.startsWith("/activity")) return t("Activity", "Aktivität");
    if (p.startsWith("/audit")) return t("Audit", "Prüfprotokoll");
    if (p.startsWith("/governance")) return t("Access & roles", "Zugriff & Rollen");
    if (p.startsWith("/members")) return t("Users", "Benutzer");
    if (p.startsWith("/models")) return t("Models", "Modelle");
    if (p.startsWith("/costs")) return t("Costs", "Kosten");
    if (p.startsWith("/statistics")) return t("Statistics", "Statistiken");
    if (p.startsWith("/settings")) return t("Settings", "Einstellungen");
    return "oc8";
  };
  const pageSubtitle = (p: string) => {
    if (p === "/")
      return t(
        "Your company as a living floor — each department a room.",
        "Ihr Unternehmen als lebendiges Stockwerk — jede Abteilung ein Raum.",
      );
    // The departments you cover, so the page says whose desk it is before you
    // read a row. Names, not ids, and never a hardcoded department: they come
    // from the caller's own seats.
    if (p.startsWith("/workspace")) {
      if (standing.unrestricted)
        return t(
          "Every department — decisions and questions waiting on a person.",
          "Alle Abteilungen — Entscheidungen und Rückfragen, die auf einen Menschen warten.",
        );
      const names = standing.seats.map((s) => s.departmentName).filter(Boolean);
      if (names.length > 0) return names.join(" · ");
      return t(
        "Decisions and questions waiting on you.",
        "Entscheidungen und Rückfragen, die auf dich warten.",
      );
    }
    if (p.startsWith("/departments"))
      return t(
        "Teams, goals, and workload at a glance.",
        "Teams, Ziele und Auslastung auf einen Blick.",
      );
    if (p.startsWith("/agents"))
      return t(
        "All agents, roles, and responsibilities.",
        "Alle Agenten, Rollen und Verantwortungen.",
      );
    if (p.startsWith("/skills"))
      return t(
        "Reusable capabilities agents can be composed from.",
        "Wiederverwendbare Fähigkeiten, aus denen Agenten zusammengesetzt werden.",
      );
    if (p.startsWith("/handoffs"))
      return t(
        "Cross-department handoffs — contracts, gates, provenance.",
        "Abteilungsübergreifende Übergaben — Verträge, Gates, Herkunft.",
      );
    if (p.startsWith("/flows"))
      return t(
        "Multi-stage flows across departments with live validation.",
        "Mehrstufige Abläufe über Abteilungen hinweg mit Live-Validierung.",
      );
    if (p.startsWith("/capas"))
      return t(
        "Every installable capability — connectors, tools, guardrails, templates.",
        "Jede installierbare Fähigkeit — Connectoren, Tools, Guardrails, Vorlagen.",
      );
    if (p.startsWith("/knowledge"))
      return t("Shared memory & private stores.", "Gemeinsames Wissen & private Speicher.");
    if (p.startsWith("/activity"))
      return t("Every action, every timestamp.", "Jede Aktion, jeder Zeitstempel.");
    if (p.startsWith("/audit"))
      return t(
        "Tamper-evident record of every decision.",
        "Manipulationssicheres Protokoll jeder Entscheidung.",
      );
    if (p.startsWith("/governance"))
      return t(
        "Who may do what — and why you were refused.",
        "Wer was darf — und warum Sie abgewiesen wurden.",
      );
    if (p.startsWith("/members"))
      return t(
        "Manage user roles for your organization.",
        "Verwalten Sie Benutzerrollen für Ihre Organisation.",
      );
    if (p.startsWith("/models"))
      return t("LLM providers, costs, and assignments.", "LLM-Anbieter, Kosten und Zuweisungen.");
    if (p.startsWith("/costs"))
      return t(
        "Token spend across agents and departments.",
        "Token-Verbrauch über Agenten und Abteilungen.",
      );
    if (p.startsWith("/statistics"))
      return t(
        "Runs, durations, and waiting time across the tenant.",
        "Runs, Dauern und Wartezeiten über die gesamte Organisation.",
      );
    return "";
  };

  const [theme, setTheme] = useState<"light" | "dark">(() => {
    if (typeof window === "undefined") return "dark";
    const stored = window.localStorage.getItem("bf-theme");
    return stored === "light" ? "light" : "dark";
  });
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", theme === "dark");
    window.localStorage.setItem("bf-theme", theme);
  }, [theme]);

  const [notifOpen, setNotifOpen] = useState(false);
  useEffect(() => {
    const n = () => setNotifOpen(true);
    window.addEventListener(OPEN_NOTIFICATIONS_EVENT, n);
    return () => window.removeEventListener(OPEN_NOTIFICATIONS_EVENT, n);
  }, []);

  useEffect(() => listenForPushSubscriptionChanges(), []);

  return (
    <div className="flex min-h-screen w-full bg-background text-foreground">
      <aside
        className={cn(
          "hidden md:flex shrink-0 flex-col border-r border-border bg-sidebar transition-[width] duration-200",
          collapsed ? "md:w-[68px]" : "md:w-64",
        )}
      >
        <div
          className={cn(
            "flex flex-col items-start gap-1.5 px-6 py-6",
            collapsed && "items-center px-0",
          )}
        >
          {/* The lockup SVG already draws "oc8" as artwork (brand sheet:
              "Wordmark -- drawn artwork, not type -- never reset in a
              font") -- a second, separately-typeset "oc8" beside it was a
              duplicate wordmark, not a second brand element. Collapsed to the
              rail, there is no room for the wordmark OR the caption -- rather
              than inventing a second, square mark, this header simply goes
              quiet until the sidebar is wide again. */}
          {!collapsed && (
            <>
              <img
                src={theme === "dark" ? "/oc8_Logo_white.svg" : "/oc8_Logo.svg"}
                alt="oc8"
                className="h-9 w-auto shrink-0 select-none"
                draggable={false}
              />
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                <span>{t("Control Panel", "Leitstand")}</span>
              </div>
            </>
          )}
        </div>

        <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-2">
          {nav.map((entry) => {
            if (!isSection(entry)) {
              return visible(entry) ? (
                <NavItemLink
                  key={entry.to}
                  item={entry}
                  pathname={pathname}
                  collapsed={collapsed}
                />
              ) : null;
            }
            // A section with nothing in it the caller may open is not rendered
            // as an empty heading: an employee who holds nothing sees Workspace
            // and Access, and Access lives in here — which is why this section
            // survives for him while its other four children do not.
            const children = entry.children.filter(visible);
            if (children.length === 0) return null;
            return (
              <NavItemSection
                key={entry.section}
                section={entry}
                items={children}
                pathname={pathname}
                collapsed={closedSections.has(entry.section)}
                onToggle={() => toggleSection(entry.section)}
                onReveal={revealSection}
                sidebarCollapsed={collapsed}
                onExpandSidebar={expandSidebar}
              />
            );
          })}
        </nav>

        <div
          className={cn(
            "border-t border-sidebar-border px-4 py-4 text-xs text-muted-foreground",
            collapsed && "flex justify-center px-0",
          )}
        >
          {collapsed ? (
            <span className="status-dot text-[color:var(--status-running)] bg-[color:var(--status-running)]" />
          ) : (
            <div className="flex items-center justify-between">
              <span className="inline-flex items-center gap-2">
                <span className="status-dot text-[color:var(--status-running)] bg-[color:var(--status-running)]" />
                {t("All systems operational", "Alle Systeme betriebsbereit")}
              </span>
              <span className="text-[10px]">v0.9</span>
            </div>
          )}
        </div>

        <div className="border-t border-sidebar-border p-2">
          <button
            type="button"
            onClick={toggleSidebar}
            aria-label={
              collapsed
                ? t("Expand sidebar", "Seitenleiste ausklappen")
                : t("Collapse sidebar", "Seitenleiste einklappen")
            }
            title={
              collapsed
                ? t("Expand sidebar", "Seitenleiste ausklappen")
                : t("Collapse sidebar", "Seitenleiste einklappen")
            }
            className={cn(
              "flex w-full items-center gap-2 rounded-md py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground",
              collapsed ? "justify-center px-2" : "px-3",
            )}
          >
            {collapsed ? (
              <PanelLeftOpen className="h-4 w-4 shrink-0" />
            ) : (
              <PanelLeftClose className="h-4 w-4 shrink-0" />
            )}
            {!collapsed && <span>{t("Collapse", "Einklappen")}</span>}
          </button>
        </div>
      </aside>

      <div className={cn("flex min-w-0 flex-1 flex-col", pathname === "/copilot" && "h-screen")}>
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-x-4 gap-y-3 border-b border-border bg-background/85 px-4 py-4 backdrop-blur md:px-8">
          {/* `min-w-[12rem]` (not `min-w-0`) on purpose: `flex-1` alone lets this
              shrink to nothing before the fixed-width controls to its right ever
              wrap, which crushed the title into a one-word-per-line column at a
              few hundred px of window width rather than the row wrapping like it
              was meant to. A real minimum forces the WHOLE block onto its own
              line once the row is actually too narrow for both. */}
          <div className="min-w-[12rem] flex-1">
            <h1 className="font-serif text-2xl leading-none md:text-3xl">{pageTitle(pathname)}</h1>
            <p className="mt-1 text-sm text-muted-foreground">{pageSubtitle(pathname)}</p>
          </div>
          <GlobalSearch />
          <button
            type="button"
            aria-label={
              theme === "dark"
                ? t("Switch to light mode", "Zu hellem Modus wechseln")
                : t("Switch to dark mode", "Zu dunklem Modus wechseln")
            }
            onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
            className="grid h-10 w-10 place-items-center rounded-md border border-border bg-panel text-muted-foreground transition hover:text-foreground"
          >
            {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </button>
          {/* Both of these used to open a sheet that had no URL. They navigate
              now, so what a person is looking at is something they can send. */}
          <Link
            to="/workspace"
            aria-label={t("Open my work", "Meine Arbeit öffnen")}
            className="relative hidden h-10 items-center gap-2 rounded-md border border-border bg-panel px-3 text-sm text-muted-foreground transition hover:text-foreground sm:inline-flex"
          >
            <ShieldCheck className="h-4 w-4" />
            <span>{t("My work", "Meine Arbeit")}</span>
            {workCount > 0 && (
              <span className="grid h-5 min-w-5 place-items-center rounded-full bg-[color:var(--status-warning)] px-1 text-[10px] font-semibold text-black">
                {workCount}
              </span>
            )}
          </Link>
          <Link
            to="/workspace"
            aria-label={t("Open my work", "Meine Arbeit öffnen")}
            className="relative grid h-10 w-10 place-items-center rounded-md border border-border bg-panel text-muted-foreground transition hover:text-foreground sm:hidden"
          >
            <ShieldCheck className="h-4 w-4" />
            {workCount > 0 && (
              <span className="absolute -right-1 -top-1 grid h-5 min-w-5 place-items-center rounded-full bg-[color:var(--status-warning)] px-1 text-[10px] font-semibold text-black">
                {workCount}
              </span>
            )}
          </Link>
          <button
            type="button"
            onClick={() => setNotifOpen(true)}
            aria-label={t("Alerts", "Benachrichtigungen")}
            className="relative grid h-10 w-10 place-items-center rounded-md border border-border bg-panel text-muted-foreground transition hover:text-foreground"
          >
            <Bell className="h-4 w-4" />
            {notifCount > 0 && (
              <span className="absolute -right-1 -top-1 grid h-5 min-w-5 place-items-center rounded-full bg-[color:var(--status-warning)] px-1 text-[10px] font-semibold text-black">
                {notifCount}
              </span>
            )}
          </button>
          {/* Was "LK" / "Lena Krüger" / "Administrator" for every caller, on
              every deployment. A workspace that narrows what you see by who you
              are cannot have somebody else's name in its corner. */}
          <div className="flex items-center gap-3 rounded-md border border-border bg-panel py-1.5 pl-1.5 pr-3">
            <Link
              to="/profile"
              aria-label={t("Profile", "Profil")}
              title={t("Profile", "Profil")}
              className="flex items-center gap-2 rounded-md transition hover:opacity-80"
            >
              <span className="grid h-7 w-7 place-items-center rounded-full bg-primary/20 text-xs font-semibold text-primary">
                {initials(standing.displayName)}
              </span>
              <div className="hidden text-xs leading-tight sm:block">
                <div className="font-medium">{standing.displayName || "—"}</div>
                <div className="text-muted-foreground">
                  {assignedRole ?? roleLabel(standing.role, t)}
                </div>
              </div>
            </Link>
            <button
              type="button"
              onClick={() => {
                void revokePushOnSignOut().finally(async () => {
                  // Community mode is checked first, mirroring api.ts's own
                  // 401-handling precedence: clearing the wrong key here
                  // left the community token in place, so "Sign out"
                  // reloaded straight back into the same authenticated
                  // session instead of actually signing out.
                  if (hasCommunitySession()) {
                    // Owns its own reload/SSO-redirect -- see its doc
                    // comment in api.ts. A reload here would race a pending
                    // SSO redirect and silently cancel it.
                    await logoutCommunity();
                  } else {
                    logoutDev();
                    window.location.reload();
                  }
                });
              }}
              aria-label={t("Sign out", "Abmelden")}
              title={t("Sign out", "Abmelden")}
              className="ml-1 inline-flex h-7 w-7 items-center justify-center rounded-md border border-border bg-background/40 text-muted-foreground transition hover:text-foreground focus:outline-none"
            >
              <LogOut className="h-3.5 w-3.5" />
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger
                aria-label={t("Change language", "Sprache ändern")}
                className="ml-1 inline-flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:text-foreground focus:outline-none"
              >
                <span className="text-sm leading-none">
                  {locales.find((l) => l.code === lang)?.flag}
                </span>
                <span className="uppercase tracking-wider">{lang}</span>
                <ChevronDown className="h-3 w-3" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
                <DropdownMenuLabel className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Sprache · Language
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                {locales.map((l) => (
                  <DropdownMenuItem
                    key={l.code}
                    onSelect={() => {
                      if (l.code === lang) return;
                      setLang(l.code);
                      toast.success(
                        `${t("Language switched to", "Sprache umgestellt auf")} ${l.nativeName}`,
                      );
                    }}
                    className="flex items-center gap-2 text-sm"
                  >
                    <span className="text-base leading-none">{l.flag}</span>
                    <span className="flex-1">{l.nativeName}</span>
                    <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                      {l.code}
                    </span>
                    {lang === l.code && <Check className="h-3.5 w-3.5 text-primary" />}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>

        <main
          className={cn(
            "min-w-0 flex-1",
            // The Copilot page owns its own scrolling: a bounded flex column
            // lets the chat fill the height under the header.
            pathname === "/copilot" ? "flex min-h-0 flex-col" : "px-4 py-6 md:px-8 md:py-8",
          )}
        >
          <Outlet />
        </main>
      </div>
      <NotificationsSheet
        open={notifOpen}
        onOpenChange={setNotifOpen}
        onOpenApprovals={() => navigate({ to: "/workspace" })}
      />
      {/*
        The app-wide <Toaster/> lives in `routes/__root.tsx`, not here: mounting
        it inside the shell meant it was created fresh on every /login -> / swap,
        and `sonner` does not replay toasts raised before a Toaster mounted. It
        still follows this theme -- root reads the same persisted "bf-theme".
      */}
      {pathname !== "/workspace" && pathname !== "/copilot" && <CopilotDock />}
    </div>
  );
}

export function Panel({
  className,
  children,
  id,
}: {
  className?: string;
  children: React.ReactNode;
  id?: string;
}) {
  return (
    <div
      id={id}
      className={cn(
        "rounded-xl border border-border bg-panel shadow-[0_1px_0_0_oklch(1_0_0/6%)_inset,0_10px_30px_-20px_oklch(0_0_0/60%)]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function StatusPill({
  status,
  className,
}: {
  status: import("@/lib/mock-data").AgentStatus;
  className?: string;
}) {
  const t = useT();
  const meta = {
    running: { label: t("running", "läuft"), color: "var(--status-running)" },
    warning: { label: t("waiting", "wartet"), color: "var(--status-warning)" },
    error: { label: t("error", "fehler"), color: "var(--status-error)" },
    paused: { label: t("paused", "pausiert"), color: "var(--status-paused)" },
    waiting_for_task: {
      label: t("Waiting for task", "Wartet auf Aufgabe"),
      color: "var(--status-waiting-for-task)",
    },
  }[status];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
        className,
      )}
      style={{
        color: meta.color,
        borderColor: `color-mix(in oklab, ${meta.color} 35%, transparent)`,
        background: `color-mix(in oklab, ${meta.color} 10%, transparent)`,
      }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: meta.color, boxShadow: `0 0 8px ${meta.color}` }}
      />
      {meta.label}
    </span>
  );
}
