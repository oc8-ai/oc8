// A connector that declares an `x-oc8-picker` property gets its scope picked in
// a <ScopePicker> -- in the source wizard and again in the edit drawer -- and a
// connector whose capa waits for consent to `knowledge:write` asks for it in
// the wizard before it can be set up. The source card says what the last sync
// skipped and what it noted. Nothing here names a vendor: the copy uses the
// connector's own label.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const postMock = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { post: (path: string, body: unknown) => postMock(path, body) },
}));

let lang: "en" | "de" = "de";
vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return { ...actual, useT: () => (en: string, de?: string) => (lang === "de" ? (de ?? en) : en) };
});

let mayManagePlugins = true;
vi.mock("@/lib/governance-hooks", () => ({
  useMay: () => (permission: string) => permission === "plugin:manage" && mayManagePlugins,
}));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({ children, to }: { children?: ReactNode; to?: string }) => <a href={to}>{children}</a>,
  };
});

vi.mock("@/lib/hooks", () => ({
  useKnowledgeBases: () => ({ data: { items: [{ id: "kb-1", name: "KB", sourceIds: [] }] } }),
  useCreateKnowledgeBase: () => ({ mutate: vi.fn(), isPending: false }),
  useCredentials: () => ({ data: [{ id: "cred-1", name: "Main login" }] }),
  useCredentialTypes: () => ({ data: [] }),
  useCreateCredential: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

const grantMock = vi.fn();
const updateSourceMock = vi.fn();
const createSourceMock = vi.fn();
let connectors: unknown[] = [];

vi.mock("@/lib/knowledge-connector-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-connector-hooks")>();
  return {
    ...actual,
    useKnowledgeConnectors: () => ({ data: connectors, isLoading: false }),
    useOAuthConnections: () => ({ data: [] }),
    useCreateSource: () => ({ mutate: createSourceMock, isPending: false }),
    useUpdateSource: () => ({ mutate: updateSourceMock, isPending: false }),
    useDeleteSource: () => ({ mutate: vi.fn(), isPending: false }),
    useSyncSource: () => ({ mutate: vi.fn(), isPending: false }),
    useIngestionJob: () => ({ data: undefined }),
    useEmbeddingModels: () => ({ data: undefined }),
    useGrantCapaPermission: () => ({ mutate: grantMock, isPending: false }),
  };
});

import { SourcesTab, SourceWizard } from "@/routes/knowledge";
import type { DataSource } from "@/lib/mock-data";

const PICKER_SCHEMA = {
  properties: {
    credential: { title: "Login", credentialType: "demo_api" },
    selection: { type: "array", title: "Selection", "x-oc8-picker": true },
  },
  required: ["credential", "selection"],
};

const PENDING = {
  typeId: "demo_docs",
  label: "Demo Docs",
  configSchema: PICKER_SCHEMA,
  requiresOauth: null,
  usable: false,
  missing: "consent:knowledge:write",
  capaId: "capa-1",
};

const WEBSITE = {
  typeId: "website",
  label: "Website",
  configSchema: { properties: {} },
  requiresOauth: null,
  usable: true,
  missing: null,
};

const ROOT = {
  entries: [
    { id: "f:1", name: "Contracts", isFolder: true, kind: "folder" },
    { id: "d:9", name: "readme.txt", isFolder: false, kind: "file" },
  ],
  truncated: false,
};

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const QUERY = {
  search: "",
  includeArchived: false,
  groupBy: null,
  sort: null,
  page: 1,
  pageSize: 25,
} as unknown as Parameters<typeof SourcesTab>[0]["queryState"];

function renderSources(sources: DataSource[]) {
  return wrap(
    <SourcesTab
      sources={sources}
      setSources={vi.fn()}
      onAdd={vi.fn()}
      queryState={QUERY}
      onQueryStateChange={vi.fn()}
      totalCount={sources.length}
    />,
  );
}

beforeEach(() => {
  lang = "de";
  mayManagePlugins = true;
  connectors = [WEBSITE, PENDING];
  grantMock.mockReset();
  updateSourceMock.mockReset();
  createSourceMock.mockReset();
  postMock.mockReset();
  postMock.mockImplementation(() => Promise.resolve(ROOT));
});

describe("SourceWizard: a connector waiting for consent", () => {
  it("lets someone who manages capas allow it, then continues", () => {
    grantMock.mockImplementation((_vars: unknown, opts: { onSuccess?: () => void }) =>
      opts.onSuccess?.(),
    );
    wrap(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Demo Docs/ }));
    expect(
      screen.getByText("Demo Docs darf Inhalte in Wissensdatenbanken schreiben"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Erlauben" }));

    expect(grantMock).toHaveBeenCalledWith(
      { capaId: "capa-1", permission: "knowledge:write" },
      expect.anything(),
    );
    expect(screen.getByText(/Connect · Demo Docs/)).toBeInTheDocument();
  });

  it("keeps the wizard on the consent step and shows why when the grant fails", () => {
    grantMock.mockImplementation((_vars: unknown, opts: { onError?: (e: Error) => void }) =>
      opts.onError?.(new Error("a deferred permission is granted on an enabled capa only")),
    );
    wrap(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Demo Docs/ }));
    fireEvent.click(screen.getByRole("button", { name: "Erlauben" }));

    expect(
      screen.getByText("a deferred permission is granted on an enabled capa only"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Connect · Demo Docs/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^continue$/i })).toBeDisabled();
  });

  it("tells an admin neutrally when the connector names no capa to allow", () => {
    connectors = [WEBSITE, { ...PENDING, capaId: null }];
    wrap(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Demo Docs/ }));
    expect(
      screen.getByText("Dieser Connector lässt sich hier nicht erlauben."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Bitte eine Admin-Person/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Erlauben" })).not.toBeInTheDocument();
  });

  it("asks someone without that right to get an admin to allow it", () => {
    mayManagePlugins = false;
    wrap(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Demo Docs/ }));
    expect(screen.getByText(/Bitte eine Admin-Person, das zu erlauben/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Erlauben" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^continue$/i })).toBeDisabled();
  });

  it("keeps the picker disabled until the credential is set", () => {
    connectors = [{ ...PENDING, usable: true, missing: null }];
    wrap(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    expect(screen.getByText("Fülle zuerst die übrigen Pflichtfelder aus.")).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
  });
});

describe("SourceWizard: picking the scope", () => {
  beforeEach(() => {
    connectors = [{ ...PENDING, usable: true, missing: null }];
  });

  it("browses once the credential is set and only continues with something picked", async () => {
    wrap(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });

    const contracts = await screen.findByRole("checkbox", { name: "Contracts" });
    expect(postMock).toHaveBeenCalledWith("/knowledge/connectors/demo_docs/browse", {
      config: { credential: "cred-1" },
      path: "/",
    });
    expect(screen.getByRole("button", { name: /^continue$/i })).toBeDisabled();
    fireEvent.click(contracts);
    expect(screen.getByRole("button", { name: /^continue$/i })).toBeEnabled();
  });

  it("keeps what was entered when the connector list is fetched again (e.g. after a grant)", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ui = () => (
      <QueryClientProvider client={qc}>
        <SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui());
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });
    await screen.findByRole("checkbox", { name: "Contracts" });

    connectors = [structuredClone({ ...PENDING, usable: true, missing: null })];
    rerender(ui());

    expect(screen.getByRole("combobox")).toHaveValue("cred-1");
  });
});

describe("SourceEditDrawer: the source's scope", () => {
  const source = {
    id: "ds-1",
    kind: "demo_docs",
    name: "Docs",
    connected: true,
    sensitivity: "internal",
    config: {
      credential: "cred-1",
      selection: [{ id: "f:1", label: "Contracts", kind: "folder" }],
    },
  } as unknown as DataSource;

  beforeEach(() => {
    connectors = [{ ...PENDING, usable: true, missing: null, capaId: null }];
  });

  it("shows the saved selection and saves a changed one as config.selection", async () => {
    renderSources([source]);
    fireEvent.click(screen.getByTitle("Konfigurieren"));

    const contracts = await screen.findByRole("checkbox", { name: "Contracts" });
    expect(contracts).toBeChecked();
    expect(postMock).toHaveBeenCalledWith("/knowledge/connectors/demo_docs/browse", {
      config: { credential: "cred-1" },
      path: "/",
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "readme.txt" }));
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));

    expect(updateSourceMock).toHaveBeenCalledWith(
      expect.objectContaining({
        sourceId: "ds-1",
        config: {
          selection: [
            { id: "f:1", label: "Contracts", kind: "folder" },
            { id: "d:9", label: "readme.txt", kind: "file" },
          ],
        },
      }),
      expect.anything(),
    );
  });

  it("blocks saving a source whose saved selection is empty, before anything is touched", async () => {
    renderSources([{ ...source, config: { credential: "cred-1" } } as DataSource]);
    fireEvent.click(screen.getByTitle("Konfigurieren"));
    await screen.findByRole("checkbox", { name: "Contracts" });
    expect(screen.getByText("Wähle mindestens einen Eintrag aus.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Speichern" })).toBeDisabled();
  });

  it("does not let an empty selection be saved", async () => {
    renderSources([source]);
    fireEvent.click(screen.getByTitle("Konfigurieren"));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Contracts" }));
    expect(screen.getByRole("button", { name: "Speichern" })).toBeDisabled();
  });

  it("shows the server's refusal and stays open", async () => {
    updateSourceMock.mockImplementation((_vars: unknown, opts: { onError?: (e: Error) => void }) =>
      opts.onError?.(new Error("select at least one item for selection")),
    );
    renderSources([source]);
    fireEvent.click(screen.getByTitle("Konfigurieren"));
    await screen.findByRole("checkbox", { name: "Contracts" });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() =>
      expect(screen.getByText("select at least one item for selection")).toBeInTheDocument(),
    );
    expect(screen.getByRole("checkbox", { name: "Contracts" })).toBeInTheDocument();
  });
});

describe("Source card: what the last sync skipped and noted", () => {
  const synced = {
    id: "ds-2",
    kind: "demo_docs",
    name: "Docs",
    connected: true,
    sensitivity: "internal",
    lastSyncSkipped: { unsupported_type: 2, too_large: 1 },
    lastSyncNotes: ["api_key_recommended", "something_new"],
  } as unknown as DataSource;

  it("counts the skipped files by reason, in German", () => {
    renderSources([synced]);
    expect(
      screen.getByText("3 übersprungen: Dateityp nicht unterstützt (2), zu groß (1)"),
    ).toBeInTheDocument();
  });

  it("counts the skipped files by reason, in English", () => {
    lang = "en";
    renderSources([synced]);
    expect(
      screen.getByText("3 skipped: unsupported file type (2), too large (1)"),
    ).toBeInTheDocument();
  });

  it("names a link that was skipped instead of a file", () => {
    renderSources([{ ...synced, lastSyncSkipped: { url_document: 4 } } as DataSource]);
    expect(screen.getByText("4 übersprungen: Link statt Datei (4)")).toBeInTheDocument();
  });

  it("renders a known note and nothing for an unknown one, in German", () => {
    const { container } = renderSources([synced]);
    expect(
      screen.getByText("Wir empfehlen einen API-Schlüssel statt eines Passworts."),
    ).toBeInTheDocument();
    expect(container.textContent).not.toContain("something_new");
  });

  it("renders a known note in English", () => {
    lang = "en";
    renderSources([synced]);
    expect(screen.getByText("We recommend an API key instead of a password.")).toBeInTheDocument();
  });

  it("says when an emptied selection hid the source's content", () => {
    renderSources([{ ...synced, lastSyncNotes: ["scope_emptied"] } as DataSource]);
    expect(
      screen.getByText(
        "Die Auswahl war beim letzten Abgleich leer – die Inhalte sind ausgeblendet und lassen sich wiederherstellen.",
      ),
    ).toBeInTheDocument();
  });

  it("says when an emptied selection hid the source's content, in English", () => {
    lang = "en";
    renderSources([{ ...synced, lastSyncNotes: ["scope_emptied"] } as DataSource]);
    expect(
      screen.getByText(
        "The selection was empty at the last sync – its content is hidden and can be restored.",
      ),
    ).toBeInTheDocument();
  });

  it("shows nothing when the last sync skipped and noted nothing", () => {
    const { container } = renderSources([
      { ...synced, lastSyncSkipped: null, lastSyncNotes: null } as DataSource,
    ]);
    expect(container.textContent).not.toMatch(/übersprungen/);
  });
});
