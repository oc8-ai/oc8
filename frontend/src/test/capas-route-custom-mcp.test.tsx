// Task 12 (custom-mcp-capa plan): wires the real CustomMcpWizard (Task 11)
// behind a trigger button on the Capas route. Follows
// capas-list-toolbar.test.tsx's own `vi.mock("@/lib/hooks", ...)` + render
// `CapasPage` convention -- extended here with the two extra hooks
// CustomMcpWizard itself calls (useInstallCustomCapa, useTestMcpConnectionById)
// so the real wizard can mount without crashing once the trigger opens it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DiscoveredCapa } from "@/lib/hooks";

const useAvailablePluginsMock = vi.fn();

vi.mock("@/lib/hooks", () => ({
  useAvailablePlugins: (params: unknown) => useAvailablePluginsMock(params),
  useMcpConnections: () => ({ data: [] }),
  useInstallPluginFromDisk: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useEnablePlugin: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDisablePlugin: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useCapaIcon: () => ({ data: null }),
  useInstallCustomCapa: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useTestMcpConnectionById: () => ({ mutateAsync: vi.fn(), isPending: false }),
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

describe("Capas route — custom MCP trigger", () => {
  beforeEach(() => {
    useAvailablePluginsMock.mockReset();
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [capa({})], totalCount: 1 },
      isLoading: false,
      error: null,
    });
  });

  it("opens the CustomMcpWizard when the 'Add custom MCP server' button is clicked", () => {
    renderPage();

    expect(screen.queryByText(/Local MCP server/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /add custom mcp server/i }));

    expect(screen.getByText(/Local MCP server/i)).toBeInTheDocument();
  });

  it("closes the wizard without opening the export dialog when the user just cancels", () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /add custom mcp server/i }));
    fireEvent.click(screen.getByRole("button", { name: /close/i }));

    expect(screen.queryByText(/Local MCP server/i)).not.toBeInTheDocument();
    // The CapaExportWizard dialog (a distinct "Export as Capa" DIALOG TITLE,
    // not the always-present toolbar button of the same name) never opened.
    expect(screen.queryByRole("heading", { name: /^export as capa$/i })).not.toBeInTheDocument();
  });
});
