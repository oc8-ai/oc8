// Task (2026-09-28, sidebar-collapse spec): the sidebar `<aside>` gets an
// icon-rail collapse toggle with a two-mechanism state split --
// `usePersistedSidebarCollapsed`'s own doc comment in app-shell.tsx has the
// full rationale. This file pins the four behaviours from that spec's test
// list:
//   1. a non-workspace route with no stored preference renders expanded
//   2. clicking the toggle there collapses AND persists to localStorage
//   3. /workspace always renders collapsed, even overriding a persisted
//      `userCollapsed: false`
//   4. expanding while on /workspace is a same-visit-only override: a fresh
//      mount (simulating a fresh navigation) starts collapsed again, and
//      never touches the persisted key
//
// Heavy, unrelated pieces of the shell (GlobalSearch, CopilotDock, the
// notifications sheet) are stubbed out entirely -- this test only cares
// about the `<aside>`, and none of the three are touched by this feature.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const routerState = vi.hoisted(() => ({ pathname: "/agents" }));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    useRouterState: (opts: { select: (s: { location: { pathname: string } }) => unknown }) =>
      opts.select({ location: { pathname: routerState.pathname } }),
    useNavigate: () => vi.fn(),
    Outlet: () => null,
    Link: ({
      children,
      to,
      className,
      title,
    }: {
      children?: ReactNode;
      to?: string;
      className?: string;
      title?: string;
    }) => (
      <a href={to} className={className} title={title}>
        {children}
      </a>
    ),
  };
});

vi.mock("@/lib/hooks", () => ({
  useAgents: () => ({ data: { items: [] } }),
  useApprovals: () => ({ data: [] }),
  useClarifications: () => ({ data: [] }),
  useStanding: () => ({
    pending: false,
    seats: [],
    unrestricted: true,
    decidesEverywhere: false,
    unassigned: false,
    displayName: "Test User",
    role: "org_admin",
  }),
}));

vi.mock("@/lib/governance-hooks", () => ({
  useCan: () => () => true,
  useMay: () => () => true,
  useAssignedRoleName: () => null,
}));

// Out of scope for this feature (brief: "Do not touch CopilotDock,
// GlobalSearch, or anything in <header>/<main>") and each pulls in its own
// unrelated hooks -- stubbed so this test only exercises the sidebar.
vi.mock("@/components/global-search", () => ({ GlobalSearch: () => null }));
vi.mock("@/components/copilot-dock", () => ({ CopilotDock: () => null }));
vi.mock("@/components/inbox-sheets", () => ({
  NotificationsSheet: () => null,
  OPEN_NOTIFICATIONS_EVENT: "oc8:open-notifications",
}));

import { AppShell } from "@/components/app-shell";

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AppShell />
    </QueryClientProvider>,
  );
}

// Scoped to the sidebar's own `<nav>` landmark -- the header renders a page
// title ("Agents", "Departments", ...) that collides with the nav's own
// link labels by plain text, so an unscoped `getByText` matches both.
function sidebarNav() {
  return within(screen.getByRole("navigation"));
}

describe("AppShell sidebar collapse", () => {
  beforeEach(() => {
    routerState.pathname = "/agents";
    localStorage.clear();
  });

  it("renders expanded on a non-workspace route with no stored preference", () => {
    renderShell();
    expect(sidebarNav().getByText("Agents")).toBeInTheDocument();
    expect(sidebarNav().getByText("Departments")).toBeInTheDocument();
  });

  it("clicking the toggle collapses the sidebar and persists the choice", () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: /collapse sidebar/i }));

    expect(sidebarNav().queryByText("Agents")).not.toBeInTheDocument();
    expect(sidebarNav().queryByText("Departments")).not.toBeInTheDocument();
    expect(localStorage.getItem("oc8-sidebar-collapsed")).toBe("1");
  });

  it("renders collapsed on /workspace even when userCollapsed is persisted false", () => {
    localStorage.setItem("oc8-sidebar-collapsed", "0");
    routerState.pathname = "/workspace";
    renderShell();

    expect(sidebarNav().queryByText("Agents")).not.toBeInTheDocument();
    expect(sidebarNav().queryByText("Departments")).not.toBeInTheDocument();
  });

  it("expanding on /workspace is a same-visit override that resets on a fresh mount", () => {
    routerState.pathname = "/workspace";
    const { unmount } = renderShell();

    fireEvent.click(screen.getByRole("button", { name: /expand sidebar/i }));
    expect(sidebarNav().getByText("Agents")).toBeInTheDocument();
    expect(sidebarNav().getByText("Departments")).toBeInTheDocument();

    unmount();
    // The workspace-local override is component state, not storage -- it
    // must not have written anything to the persisted key.
    expect(localStorage.getItem("oc8-sidebar-collapsed")).toBeNull();

    renderShell();
    expect(sidebarNav().queryByText("Agents")).not.toBeInTheDocument();
    expect(sidebarNav().queryByText("Departments")).not.toBeInTheDocument();
    expect(localStorage.getItem("oc8-sidebar-collapsed")).toBeNull();
  });
});
