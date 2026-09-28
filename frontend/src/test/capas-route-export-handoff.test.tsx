// Task 12: the second half of the wizard-to-export wiring -- proves
// routes/capas.tsx turns CustomMcpWizard's `onExported(capaId)` into a
// pre-selected CapaExportWizard open, without re-driving the (already
// covered, in custom-mcp-wizard.test.tsx / capa-export-wizard.test.tsx) real
// wizard internals. Both children are stubbed to isolate the route's own
// prop wiring: what id/kind/name reaches CapaExportWizard's
// `initialSelection`, and that closing it clears the target again.
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
}));

vi.mock("@/components/custom-mcp-wizard", () => ({
  CustomMcpWizard: ({
    open,
    onExported,
  }: {
    open: boolean;
    onExported?: (capaId: string) => void;
  }) =>
    open ? (
      <button type="button" onClick={() => onExported?.("capa-77")}>
        mock-finish-and-export
      </button>
    ) : null,
}));

vi.mock("@/components/capa-export-wizard", () => ({
  CapaExportWizard: ({
    initialSelection,
  }: {
    initialSelection?: { kind: "tool_pack"; id: string; name: string };
  }) => (
    <div data-testid="export-wizard">
      {initialSelection
        ? `preselected:${initialSelection.kind}:${initialSelection.id}:${initialSelection.name}`
        : "no-preselection"}
    </div>
  ),
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

describe("Capas route — custom MCP export hand-off", () => {
  beforeEach(() => {
    useAvailablePluginsMock.mockReset();
    useAvailablePluginsMock.mockReturnValue({
      data: { items: [], totalCount: 0 },
      isLoading: false,
      error: null,
    });
  });

  it("opens CapaExportWizard pre-selected on the freshly installed capa's id", () => {
    useAvailablePluginsMock.mockReturnValue({
      data: {
        items: [
          capa({
            pluginId: "custom_one",
            name: "custom_one",
            label: "My HTTP API",
            databaseId: "capa-77",
          }),
        ],
        totalCount: 1,
      },
      isLoading: false,
      error: null,
    });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /add custom mcp server/i }));
    fireEvent.click(screen.getByText("mock-finish-and-export"));

    // The wizard itself is gone (route re-set customMcpOpen to false)...
    expect(screen.queryByText("mock-finish-and-export")).not.toBeInTheDocument();
    // ...and CapaExportWizard opened with that same capa id, resolved to its
    // display label from the route's own already-loaded plugins list.
    expect(screen.getByTestId("export-wizard")).toHaveTextContent(
      "preselected:tool_pack:capa-77:My HTTP API",
    );
  });

  it("falls back to the raw id as a label when the new capa isn't in the loaded plugins list yet", () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /add custom mcp server/i }));
    fireEvent.click(screen.getByText("mock-finish-and-export"));

    expect(screen.getByTestId("export-wizard")).toHaveTextContent(
      "preselected:tool_pack:capa-77:capa-77",
    );
  });

  it("clears the pre-selection once the export dialog is closed, so a later unrelated export starts clean", () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /add custom mcp server/i }));
    fireEvent.click(screen.getByText("mock-finish-and-export"));
    expect(screen.getByTestId("export-wizard")).toHaveTextContent("preselected:tool_pack:capa-77");

    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(screen.queryByTestId("export-wizard")).not.toBeInTheDocument();

    // Reopening "Export as Capa" directly (not via the custom MCP hand-off)
    // must not still be carrying the old target capa id.
    fireEvent.click(screen.getByRole("button", { name: /^export as capa$/i }));
    expect(screen.getByTestId("export-wizard")).toHaveTextContent("no-preselection");
  });
});
