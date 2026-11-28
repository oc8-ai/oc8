// Task 17 (Design System Consistency plan): the Capas list page now goes
// through <ListToolbar> (Task 14) instead of the old hand-rolled Installed/
// Available filtering alone -- server-side search/filter/group/page state
// lives in a local `ListQueryState` and is threaded straight into
// `useAvailablePlugins(params)` (Task 15). The existing Installed/Available
// tab UI stays as an ADDITIONAL filter layer on top of that.
//
// Unlike Skills (Task 16), Capas have no server-side archive concept -- Task
// 8 deliberately left `/capas/available` without one, since it's a disk scan
// annotated per-tenant, not a `SoftDeleteMixin` table. So "Show disabled" is
// a purely CLIENT-SIDE filter, scoped to the Installed tab, over items where
// `installed && installationStatus` is "disabled" or "quarantined" -- the
// closest analog to "archived" here -- and its "Restore" action reuses the
// EXISTING `useEnablePlugin()` mutation rather than a new restore endpoint.
//
// Fix-round note: `installationStatus === "enabled"` was the ORIGINAL (wrong)
// predicate for "archived" -- `install_from_disk` never auto-enables a capa,
// so every freshly-installed capa lands in "installed" and stays there until
// a separate `useEnablePlugin()` call, meaning that predicate hid EVERY
// just-installed capa by default. The predicate is now `status === "disabled"
// || status === "quarantined"` (see `isArchivedInstall` in capas.tsx), so
// "installed" (never yet enabled) shows inline with its normal Enable
// affordance, matching pre-Task-17 behavior for that state.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DiscoveredCapa } from "@/lib/hooks";

const useAvailablePluginsMock = vi.fn();
const enableMock = vi.fn();

vi.mock("@/lib/hooks", () => ({
  useAvailablePlugins: (params: unknown) => useAvailablePluginsMock(params),
  useMcpConnections: () => ({ data: [] }),
  useInstallPluginFromDisk: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useEnablePlugin: () => ({ mutateAsync: enableMock, isPending: false }),
  useDisablePlugin: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useCapaIcon: () => ({ data: null }),
}));

import { CapasPage } from "@/routes/capas";

function capa(overrides: Partial<DiscoveredCapa>): DiscoveredCapa {
  return {
    pluginId: "p",
    name: "p",
    label: "P",
    version: "1.0.0",
    type: "connector",
    trust: "first_party",
    summary: "",
    valid: true,
    installed: false,
    installedVersion: null,
    databaseId: null,
    installationStatus: null,
    permissions: [],
    capabilities: [],
    surfaces: [],
    setup: null,
    ...overrides,
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CapasPage />
    </QueryClientProvider>,
  );
}

describe("Capas list page", () => {
  beforeEach(() => {
    useAvailablePluginsMock.mockReset();
    enableMock.mockReset();
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [], totalCount: 0 },
      isLoading: false,
      error: null,
    });
  });

  it("re-fetches with the typed search term", () => {
    renderPage();

    fireEvent.change(screen.getByPlaceholderText(/search capas/i), {
      target: { value: "github" },
    });

    const lastCall = useAvailablePluginsMock.mock.calls.at(-1)?.[0];
    expect(lastCall).toMatchObject({ search: "github" });
  });

  it("hides a disabled-but-installed capa by default, and shows it with a Restore action once 'Show disabled' is checked", () => {
    const enabledCapa = capa({
      pluginId: "enabled_one",
      name: "enabled_one",
      label: "Enabled One",
      installed: true,
      databaseId: "db-1",
      installationStatus: "enabled",
    });
    const disabledCapa = capa({
      pluginId: "disabled_one",
      name: "disabled_one",
      label: "Disabled One",
      installed: true,
      databaseId: "db-2",
      installationStatus: "disabled",
    });
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [enabledCapa, disabledCapa], totalCount: 2 },
      isLoading: false,
      error: null,
    });

    renderPage();

    // "Installed" is the default tab -- the disabled-but-installed capa is
    // hidden until "Show disabled" is checked, same as an archived skill.
    expect(screen.getByText("Enabled One")).toBeInTheDocument();
    expect(screen.queryByText("Disabled One")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText(/show disabled/i));

    expect(screen.getByText("Disabled One")).toBeInTheDocument();
    const restoreButton = screen.getByRole("button", { name: /restore/i });
    expect(restoreButton).toBeInTheDocument();

    fireEvent.click(restoreButton);
    // Restore is the EXISTING useEnablePlugin() mutation, not a new endpoint.
    expect(enableMock).toHaveBeenCalledWith({ pluginId: "db-2", grantedPermissions: [] });
  });

  it("restores without granting a permission the capa defers to its own consent step", () => {
    useAvailablePluginsMock.mockReturnValue({
      data: {
        items: [
          capa({
            pluginId: "deferring",
            name: "deferring",
            label: "Deferring",
            installed: true,
            databaseId: "db-4",
            installationStatus: "disabled",
            permissions: ["mcp:connect", "knowledge:write"],
            deferredPermissions: ["knowledge:write"],
          }),
        ],
        totalCount: 1,
      },
      isLoading: false,
      error: null,
    });

    renderPage();
    fireEvent.click(screen.getByText(/show disabled/i));
    fireEvent.click(screen.getByRole("button", { name: /restore/i }));

    expect(enableMock).toHaveBeenCalledWith({
      pluginId: "db-4",
      grantedPermissions: ["mcp:connect"],
    });
  });

  it("hides a quarantined-but-installed capa by default, same as a disabled one", () => {
    const quarantinedCapa = capa({
      pluginId: "quarantined_one",
      name: "quarantined_one",
      label: "Quarantined One",
      installed: true,
      databaseId: "db-3",
      installationStatus: "quarantined",
    });
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [quarantinedCapa], totalCount: 1 },
      isLoading: false,
      error: null,
    });

    renderPage();

    expect(screen.queryByText("Quarantined One")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText(/show disabled/i));

    expect(screen.getByText("Quarantined One")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /restore/i })).toBeInTheDocument();
  });

  it("shows a freshly-installed-but-never-enabled capa inline by default, with its normal Enable affordance, NOT hidden behind 'Show disabled'", () => {
    // `install_from_disk` never auto-enables a capa -- every install lands
    // in installationStatus "installed" and stays there until a separate
    // useEnablePlugin() call. This must NOT be treated as "archived": it's
    // the ordinary, expected resting state right after installing.
    const justInstalledCapa = capa({
      pluginId: "just_installed",
      name: "just_installed",
      label: "Just Installed",
      installed: true,
      installedVersion: "1.0.0",
      databaseId: "db-4",
      installationStatus: "installed",
    });
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [justInstalledCapa], totalCount: 1 },
      isLoading: false,
      error: null,
    });

    renderPage();

    // Visible without checking "Show disabled" at all.
    expect(screen.getByText("Just Installed")).toBeInTheDocument();
    // No "Disabled" badge, and the normal Enable button (not Restore).
    expect(screen.queryByText(/^disabled$/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /restore/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^enable$/i })).toBeInTheDocument();
  });

  it("shows a capa disabled for 'plugin update pending consent' inline by default, with an Update-pending badge and the normal Enable button, NOT hidden behind 'Show disabled'", () => {
    // Live user report, 2026-08-24: clicking Update disables the OLD version
    // pending re-consent (install_from_disk's own two-step flow) -- a normal,
    // temporary state right after Update, not something the user asked to
    // hide. Treating it the same as a deliberate disable made the capa
    // disappear from the list entirely.
    const updatePendingCapa = capa({
      pluginId: "update_pending",
      name: "update_pending",
      label: "Odoo",
      version: "1.8.4",
      installed: true,
      installedVersion: "1.8.4",
      databaseId: "db-7",
      installationStatus: "disabled",
      disabledReason: "plugin update pending consent",
    });
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [updatePendingCapa], totalCount: 1 },
      isLoading: false,
      error: null,
    });

    renderPage();

    expect(screen.getByText("Odoo")).toBeInTheDocument();
    expect(screen.getByText(/update pending/i)).toBeInTheDocument();
    expect(screen.queryByText(/^disabled$/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /restore/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^enable$/i })).toBeInTheDocument();
  });

  it("still hides a capa disabled for any OTHER reason, distinct from update-pending-consent", () => {
    const deliberatelyDisabledCapa = capa({
      pluginId: "operator_disabled",
      name: "operator_disabled",
      label: "Operator Disabled",
      installed: true,
      databaseId: "db-8",
      installationStatus: "disabled",
      disabledReason: "circuit breaker: too many hook failures",
    });
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [deliberatelyDisabledCapa], totalCount: 1 },
      isLoading: false,
      error: null,
    });

    renderPage();

    expect(screen.queryByText("Operator Disabled")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/show disabled/i));
    expect(screen.getByText("Operator Disabled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /restore/i })).toBeInTheDocument();
  });

  it("does not show the 'Show disabled' toggle on the Available tab", () => {
    renderPage();

    fireEvent.click(screen.getByText(/^available/i));

    expect(screen.queryByText(/show disabled/i)).not.toBeInTheDocument();
  });

  // Task 13: the configure gear on the card itself, not just the footer
  // button inside CapaDetailSheet (reachable only via the ⓘ icon) -- the
  // same canConfigure condition (installed && enabled && a setup contract),
  // now hoisted so CapaCard can check it too.
  it("shows a gear-icon configure button on the card when the plugin can be configured, and hides it otherwise", () => {
    const configurable = capa({
      pluginId: "telegram_approvals",
      name: "telegram_approvals",
      label: "Telegram Approvals",
      installed: true,
      databaseId: "db-5",
      installationStatus: "enabled",
      setup: {
        title: "Telegram verbinden",
        description: "",
        submit_label: "Speichern & testen",
        fields: [],
      },
    });
    const notConfigurable = capa({
      pluginId: "no_setup_capa",
      name: "no_setup_capa",
      label: "No Setup Capa",
      installed: true,
      databaseId: "db-6",
      installationStatus: "enabled",
      setup: null,
    });
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [configurable, notConfigurable], totalCount: 2 },
      isLoading: false,
      error: null,
    });

    renderPage();

    // `.rounded-xl.border.border-border.bg-panel` is <Panel>'s own root
    // class combo (frontend/src/components/app-shell.tsx) -- scoping through
    // it, rather than asserting on the page as a whole, is what makes this
    // a per-card assertion instead of "a gear button exists somewhere".
    const configurableCard = screen
      .getByText("Telegram Approvals")
      .closest(".rounded-xl.border.border-border.bg-panel") as HTMLElement;
    const notConfigurableCard = screen
      .getByText("No Setup Capa")
      .closest(".rounded-xl.border.border-border.bg-panel") as HTMLElement;
    expect(configurableCard).not.toBeNull();
    expect(notConfigurableCard).not.toBeNull();

    expect(within(configurableCard).getByTitle("Telegram verbinden")).toBeInTheDocument();
    expect(within(notConfigurableCard).queryByTitle("Telegram verbinden")).not.toBeInTheDocument();
    // The absent card still has its ⓘ icon -- only the gear is conditional.
    expect(
      within(notConfigurableCard).getByTitle(/more info|weitere informationen/i),
    ).toBeInTheDocument();
  });

  it("asks the server for the whole catalog, not a 200-row slice of it", () => {
    renderPage();
    const lastCall = useAvailablePluginsMock.mock.calls.at(-1)?.[0] as { pageSize: number };
    expect(lastCall.pageSize).toBeGreaterThan(200);
  });

  it("filters by type in the browser and counts each chip, so a type is never an empty surprise", () => {
    useAvailablePluginsMock.mockReturnValue({
      data: {
        items: [
          capa({ pluginId: "odoo", name: "odoo", label: "Odoo", type: "tool_pack" }),
          capa({ pluginId: "jira", name: "jira", label: "Jira", type: "tool_pack" }),
          capa({ pluginId: "sales", name: "sales", label: "Sales", type: "agent_template" }),
        ],
        totalCount: 3,
      },
      isLoading: false,
      error: null,
    });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /^Available/ }));

    // Types with nothing on this tab get no chip; the others say how many.
    expect(screen.queryByRole("button", { name: /^Flow template/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Tool pack.*2/ }));

    expect(screen.getByText("Odoo")).toBeInTheDocument();
    expect(screen.getByText("Jira")).toBeInTheDocument();
    expect(screen.queryByText("Sales")).toBeNull();
    // The server is not asked to narrow by type: the counts need the whole tab.
    const lastCall = useAvailablePluginsMock.mock.calls.at(-1)?.[0] as {
      filters: Record<string, string>;
    };
    expect(lastCall.filters.type).toBeUndefined();
  });

  it("offers no trust chip that no capa carries", () => {
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [capa({ pluginId: "a", name: "a", label: "A" })], totalCount: 1 },
      isLoading: false,
      error: null,
    });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /^Available/ }));
    expect(screen.queryByRole("button", { name: /^Verified/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Community/ })).toBeNull();
  });

  it("says where to look when a filter on Installed finds nothing", () => {
    useAvailablePluginsMock.mockReturnValue({
      data: {
        items: [capa({ pluginId: "odoo", name: "odoo", label: "Odoo", type: "tool_pack" })],
        totalCount: 1,
      },
      isLoading: false,
      error: null,
    });
    renderPage();
    expect(screen.getByText(/available tab/i)).toBeInTheDocument();
  });
});
