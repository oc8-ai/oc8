// Task 18 (Design System Consistency plan): the Agents list page now goes
// through <ListToolbar> (Task 14) instead of the old hand-rolled search box
// -- server-side search/filter/group/page/archive state lives in a local
// `ListQueryState` and is threaded straight into `useAgents(params)` (Task
// 15). Mirrors Task 16's `skills-list-toolbar.test.tsx`.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Only the archived-row-not-clickable test below needs actual `<a>` markup to
// assert against -- `<Link>` from @tanstack/react-router throws without a
// <RouterProvider> in the tree, which this lightweight render tree (like
// every other *-list-toolbar test) deliberately doesn't set up. Stubbing it
// as a plain anchor keeps the real component tree (including the desktop
// table's archived/non-archived branch) exercised without that dependency.
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({
      children,
      className,
    }: {
      children?: ReactNode;
      className?: string;
      to?: string;
      params?: unknown;
    }) => <a className={className}>{children}</a>,
  };
});

const useAgentsMock = vi.fn();

vi.mock("@/lib/hooks", () => ({
  useAgents: (params: unknown) => useAgentsMock(params),
  useDepartments: () => ({ data: { items: [], totalCount: 0 } }),
  useRestoreAgent: () => ({ mutate: vi.fn(), isPending: false }),
  // Pulled in transitively by <NewAgentDialog>, rendered (but closed) on
  // every page load.
  useModels: () => ({ data: [] }),
  useModelProviders: () => ({ data: [] }),
  useMcpConnections: () => ({ data: [] }),
  useMcpLogins: () => ({ data: [] }),
  useCredentialTypes: () => ({ data: [] }),
  useCredentials: () => ({ data: [] }),
  useCreateMcpLogin: () => ({ mutateAsync: vi.fn() }),
  useDepartmentTools: () => ({ data: { tools: {} } }),
  useCreateAgent: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useCreateAgentTrigger: () => ({ mutateAsync: vi.fn() }),
  useAuth: () => ({ data: undefined }),
}));

import { AgentsPage } from "@/routes/agents.index";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AgentsPage />
    </QueryClientProvider>,
  );
}

describe("Agents list page", () => {
  beforeEach(() => {
    useAgentsMock.mockReset();
    useAgentsMock.mockReturnValue({ data: { items: [], totalCount: 0 } });
  });

  it("re-fetches with the typed search term", async () => {
    renderPage();

    fireEvent.change(screen.getByPlaceholderText(/search agents/i), {
      target: { value: "vera" },
    });

    const lastCall = useAgentsMock.mock.calls.at(-1)?.[0];
    expect(lastCall).toMatchObject({ search: "vera" });
  });

  it("shows the archived toggle and re-fetches with includeArchived when checked", async () => {
    renderPage();

    fireEvent.click(screen.getByText(/show archived/i));

    const lastCall = useAgentsMock.mock.calls.at(-1)?.[0];
    expect(lastCall).toMatchObject({ includeArchived: true });
  });

  // Fix-round regression test (whole-branch review finding): the desktop
  // table used to wrap EVERY row in <Link to="/agents/$id">, including
  // archived ones -- but `visible_agent` (agents/repo.py) filters
  // `deleted_at IS NULL`, so an archived agent's detail route 404s. The
  // mobile-card branch already special-cased this correctly; the desktop
  // table now mirrors it.
  it("does not wrap an archived agent's desktop row in a Link, unlike an active agent's row", () => {
    useAgentsMock.mockReturnValue({
      data: {
        items: [
          {
            id: "agent-active",
            name: "Vera",
            role: "SDR",
            avatarColor: "#0af",
            status: "running",
            llm: "gpt-4",
            tools: [],
            lastRun: "2m ago",
            tasksToday: 3,
          },
          {
            id: "agent-archived",
            name: "Otto",
            role: "SDR",
            avatarColor: "#0af",
            status: "paused",
            llm: "gpt-4",
            tools: [],
            lastRun: "1d ago",
            tasksToday: 0,
            deletedAt: "2026-08-01T00:00:00Z",
          },
        ],
        totalCount: 2,
      },
    });

    renderPage();

    fireEvent.click(screen.getByText(/show archived/i));

    // Both the desktop table and the mobile card list render in JSDOM at
    // once (only CSS breakpoints hide one) -- scope to the table (the only
    // <table> on the page) so this doesn't also pick up the mobile card,
    // which already handled the archived case correctly before this fix.
    const table = screen.getByRole("table");
    const activeName = within(table).getByText("Vera");
    const archivedName = within(table).getByText("Otto");

    expect(activeName.closest("a")).not.toBeNull();
    expect(archivedName.closest("a")).toBeNull();
  });
});
