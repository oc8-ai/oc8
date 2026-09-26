// Coverage for the "cluster several data sources into a knowledge base" UI
// gap: editing a DataSource's metadata (SourceEditDrawer, replacing the
// dead "Configure" stub) and the KbDetailDrawer's new Sources section
// (list real linked sources, add an existing one via the sync mechanism,
// remove one via the new unlink endpoint, both with real confirmation).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const updateSourceMock = vi.fn();
const deleteSourceMock = vi.fn();
const updateKnowledgeBaseMock = vi.fn();
const deleteKnowledgeBaseMock = vi.fn();
const credentialsMock = vi.fn();
const credentialTypesMock = vi.fn();
const createCredentialMock = vi.fn();
const unlinkSourceMock = vi.fn();
const syncSourceMock = vi.fn();
const createSourceMock = vi.fn();
const createKnowledgeBaseMock = vi.fn();
const knowledgeBasesMock = vi.fn();

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({ children, className }: { children?: ReactNode; className?: string }) => (
      <a className={className}>{children}</a>
    ),
  };
});

const salesKb = {
  id: "kb-1",
  name: "Sales KB",
  description: "Pricing, playbooks, objection handling.",
  sourceIds: ["ds-linked"],
  docs: 10,
  chunks: 100,
  embeddingModel: "google/gemini-embedding-2",
  sensitivity: "internal",
  updated: "2h ago",
  status: "current",
  linkedDepartments: [],
  linkedAgents: [],
  roles: ["org_admin"],
};

const emptyKb = {
  ...salesKb,
  id: "kb-empty",
  name: "Empty KB",
  sourceIds: [],
  docs: 0,
  chunks: 0,
  embeddingModel: "local/bge-large",
};

const linkedSource = {
  id: "ds-linked",
  kind: "website",
  name: "Marketing site",
  connected: true,
  sensitivity: "internal",
};

const unlinkedSource = {
  id: "ds-unlinked",
  kind: "upload",
  name: "Onboarding PDF",
  connected: true,
  sensitivity: "internal",
};

vi.mock("@/lib/hooks", () => ({
  useKnowledgeBases: (params: unknown) => knowledgeBasesMock(params),
  useDepartments: () => ({ data: { items: [], totalCount: 0 } }),
  useAgents: () => ({ data: { items: [], totalCount: 0 } }),
  useDataSources: () => ({
    data: { items: [linkedSource, unlinkedSource], totalCount: 2 },
  }),
  useUpdateKnowledgeBase: () => ({ mutate: updateKnowledgeBaseMock, isPending: false }),
  useDeleteKnowledgeBase: () => ({ mutate: deleteKnowledgeBaseMock, isPending: false }),
  useCredentials: (credentialType?: string) => credentialsMock(credentialType),
  useCredentialTypes: () => credentialTypesMock(),
  useCreateCredential: () => ({ mutateAsync: createCredentialMock, isPending: false }),
  useUnlinkSourceFromBase: () => ({
    mutate: unlinkSourceMock,
    isPending: false,
  }),
  useCreateKnowledgeBase: () => ({
    mutate: createKnowledgeBaseMock,
    isPending: false,
  }),
}));

// Hoisted, stable references -- a `mockImplementation`/inline object literal
// returning a FRESH array/object on every call breaks SourceWizard's own
// `useEffect(() => {...}, [connectors, kind])` and
// `useEffect(() => {...}, [kind, schema])` (a new `connectors`/`schema`
// reference every render re-fires the effect and re-triggers setState every
// time, an infinite render loop -- the exact class of bug this session
// already hit once tonight elsewhere).
const STUB_CONNECTOR = {
  typeId: "website",
  label: "Website",
  configSchema: { properties: {} },
};
const STUB_CONNECTORS_RESPONSE = { data: [STUB_CONNECTOR], isLoading: false };
const EMPTY_OAUTH_RESPONSE = { data: [] };

// S3-shaped fixture for the credential-picker tests below -- mirrors the
// real connector's `credential` field marked `credentialType: "s3_api"`
// (capas/s3_source/connector/connector.py), post credentials-framework
// migration.
const S3_LIKE_CONNECTOR = {
  typeId: "s3",
  label: "S3 / object storage",
  configSchema: {
    properties: {
      bucket: { type: "string", title: "Bucket" },
      credential: { type: "string", title: "S3 credentials", credentialType: "s3_api" },
    },
    required: ["bucket", "credential"],
  },
};
const S3_CONNECTORS_RESPONSE = { data: [S3_LIKE_CONNECTOR], isLoading: false };

// Mirrors capas/s3_source/credential_types/s3_api.toml.
const S3_CREDENTIAL_TYPE = {
  name: "s3_api",
  displayName: "S3 / Object storage",
  fields: [
    {
      key: "endpoint",
      label: "Endpoint",
      kind: "url",
      required: false,
      default: "",
      placeholder: "",
      help: "Only for S3-compatible stores (MinIO, Ceph). Leave empty for AWS.",
    },
    {
      key: "region",
      label: "Region",
      kind: "text",
      required: true,
      default: "us-east-1",
      placeholder: "",
      help: "",
    },
    {
      key: "access_key",
      label: "Access key",
      kind: "password",
      required: true,
      default: "",
      placeholder: "",
      help: "",
    },
    {
      key: "secret_key",
      label: "Secret key",
      kind: "password",
      required: true,
      default: "",
      placeholder: "",
      help: "",
    },
  ],
};
const EXISTING_CREDENTIAL = { id: "cred-1", name: "Prod S3", credentialType: "s3_api" };

const knowledgeConnectorsMock = vi.fn();
const knowledgeVectorIndexesMock = vi.fn();

vi.mock("@/lib/knowledge-connector-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-connector-hooks")>();
  return {
    ...actual,
    useSyncSource: () => ({ mutate: syncSourceMock, isPending: false }),
    useUpdateSource: () => ({ mutate: updateSourceMock, isPending: false }),
    useDeleteSource: () => ({ mutate: deleteSourceMock, isPending: false }),
    useCreateSource: () => ({ mutate: createSourceMock, isPending: false }),
    useKnowledgeConnectors: () => knowledgeConnectorsMock(),
    useKnowledgeVectorIndexes: () => knowledgeVectorIndexesMock(),
    useOAuthConnections: () => EMPTY_OAUTH_RESPONSE,
  };
});

import { BasesTab, BaseWizard, KbDetailDrawer, SourcesTab, SourceWizard } from "@/routes/knowledge";

function renderWithClient(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{children}</QueryClientProvider>);
}

beforeEach(() => {
  knowledgeBasesMock.mockReset();
  knowledgeBasesMock.mockReturnValue({ data: { items: [salesKb], totalCount: 1 } });
  credentialsMock.mockReset();
  credentialsMock.mockReturnValue({ data: [EXISTING_CREDENTIAL] });
  credentialTypesMock.mockReset();
  credentialTypesMock.mockReturnValue({ data: [S3_CREDENTIAL_TYPE] });
  createCredentialMock.mockReset();
  createCredentialMock.mockResolvedValue({ id: "new-cred-id" });
  knowledgeConnectorsMock.mockReset();
  knowledgeConnectorsMock.mockReturnValue(STUB_CONNECTORS_RESPONSE);
  knowledgeVectorIndexesMock.mockReset();
  knowledgeVectorIndexesMock.mockReturnValue({ data: [], isLoading: false });
});

describe("SourceEditDrawer", () => {
  beforeEach(() => {
    updateSourceMock.mockReset();
    deleteSourceMock.mockReset();
  });

  it("opens from the Configure button and saves an edited name", () => {
    renderWithClient(
      <SourcesTab
        sources={[linkedSource]}
        setSources={vi.fn()}
        onAdd={vi.fn()}
        queryState={{
          search: "",
          filters: {},
          groupBy: null,
          includeArchived: false,
          page: 1,
          pageSize: 20,
        }}
        onQueryStateChange={vi.fn()}
        totalCount={1}
      />,
    );

    fireEvent.click(screen.getByTitle(/configure/i));
    expect(screen.getByText(/edit data source/i)).toBeInTheDocument();

    const nameInput = screen.getByDisplayValue("Marketing site");
    fireEvent.change(nameInput, { target: { value: "Marketing site (renamed)" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(updateSourceMock).toHaveBeenCalledWith(
      { sourceId: "ds-linked", name: "Marketing site (renamed)", classification: "internal" },
      expect.anything(),
    );
  });

  it("does not delete when the confirmation is dismissed", async () => {
    renderWithClient(
      <SourcesTab
        sources={[linkedSource]}
        setSources={vi.fn()}
        onAdd={vi.fn()}
        queryState={{
          search: "",
          filters: {},
          groupBy: null,
          includeArchived: false,
          page: 1,
          pageSize: 20,
        }}
        onQueryStateChange={vi.fn()}
        totalCount={1}
      />,
    );

    fireEvent.click(screen.getByTitle(/configure/i));
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    // Flush the confirm dialog's resolved promise before asserting a
    // negative -- there is nothing observable to `waitFor` on here.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(deleteSourceMock).not.toHaveBeenCalled();
  });

  it("deletes the source once the confirmation is accepted", async () => {
    renderWithClient(
      <SourcesTab
        sources={[linkedSource]}
        setSources={vi.fn()}
        onAdd={vi.fn()}
        queryState={{
          search: "",
          filters: {},
          groupBy: null,
          includeArchived: false,
          page: 1,
          pageSize: 20,
        }}
        onQueryStateChange={vi.fn()}
        totalCount={1}
      />,
    );

    fireEvent.click(screen.getByTitle(/configure/i));
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    // The confirm dialog's own action button shares the "Delete" label with
    // the row's trigger button, so two matches exist once it's open.
    const deleteButtons = screen.getAllByRole("button", { name: /^delete$/i });
    fireEvent.click(deleteButtons[deleteButtons.length - 1]);

    await vi.waitFor(() =>
      expect(deleteSourceMock).toHaveBeenCalledWith("ds-linked", expect.anything()),
    );
  });

  it("shows a credential picker for a connector's credential-typed config field, prefilled from the source's current config", () => {
    knowledgeConnectorsMock.mockReturnValue(S3_CONNECTORS_RESPONSE);
    const s3Source = {
      id: "ds-s3",
      kind: "s3",
      name: "Reports bucket",
      connected: true,
      sensitivity: "internal",
      config: { bucket: "reports", credential: "cred-1" },
    };
    renderWithClient(
      <SourcesTab
        sources={[s3Source]}
        setSources={vi.fn()}
        onAdd={vi.fn()}
        queryState={{
          search: "",
          filters: {},
          groupBy: null,
          includeArchived: false,
          page: 1,
          pageSize: 20,
        }}
        onQueryStateChange={vi.fn()}
        totalCount={1}
      />,
    );

    fireEvent.click(screen.getByTitle(/configure/i));
    // The existing credential is pre-selected in the picker's dropdown.
    const comboboxes = screen.getAllByRole("combobox");
    const select = comboboxes[comboboxes.length - 1] as HTMLSelectElement;
    expect(select.value).toBe("cred-1");

    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(updateSourceMock).toHaveBeenCalledWith(
      { sourceId: "ds-s3", name: "Reports bucket", classification: "internal" },
      expect.anything(),
    );
  });

  it("submits the newly selected credential id, merged into config, when the picker's selection changes", () => {
    knowledgeConnectorsMock.mockReturnValue(S3_CONNECTORS_RESPONSE);
    credentialsMock.mockReturnValue({
      data: [EXISTING_CREDENTIAL, { id: "cred-2", name: "Backup S3", credentialType: "s3_api" }],
    });
    const s3Source = {
      id: "ds-s3",
      kind: "s3",
      name: "Reports bucket",
      connected: true,
      sensitivity: "internal",
      config: { bucket: "reports", credential: "cred-1" },
    };
    renderWithClient(
      <SourcesTab
        sources={[s3Source]}
        setSources={vi.fn()}
        onAdd={vi.fn()}
        queryState={{
          search: "",
          filters: {},
          groupBy: null,
          includeArchived: false,
          page: 1,
          pageSize: 20,
        }}
        onQueryStateChange={vi.fn()}
        totalCount={1}
      />,
    );

    fireEvent.click(screen.getByTitle(/configure/i));
    const credentialCombobox = screen.getAllByRole("combobox").at(-1) as HTMLSelectElement;
    fireEvent.change(credentialCombobox, { target: { value: "cred-2" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(updateSourceMock).toHaveBeenCalledWith(
      {
        sourceId: "ds-s3",
        name: "Reports bucket",
        classification: "internal",
        config: { credential: "cred-2" },
      },
      expect.anything(),
    );
  });
});

describe("KbDetailDrawer Sources section", () => {
  beforeEach(() => {
    unlinkSourceMock.mockReset();
    syncSourceMock.mockReset();
  });

  it("lists the real linked source resolved from sourceIds", () => {
    renderWithClient(<KbDetailDrawer kbId="kb-1" onClose={vi.fn()} onSync={vi.fn()} />);
    expect(screen.getByText("Marketing site")).toBeInTheDocument();
  });

  it("declining the remove confirmation does not call unlink", async () => {
    renderWithClient(<KbDetailDrawer kbId="kb-1" onClose={vi.fn()} onSync={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    // Flush the confirm dialog's resolved promise before asserting a
    // negative -- there is nothing observable to `waitFor` on here.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(unlinkSourceMock).not.toHaveBeenCalled();
  });

  it("confirming the remove warns about breaking agents and calls unlink with the right pair", async () => {
    renderWithClient(<KbDetailDrawer kbId="kb-1" onClose={vi.fn()} onSync={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));
    expect(screen.getByText(/immediately lose access/i)).toBeInTheDocument();
    // The confirm dialog's own action button shares the "Remove" label with
    // the row's trigger button, so two matches exist once it's open.
    const removeButtons = screen.getAllByRole("button", { name: /^remove$/i });
    fireEvent.click(removeButtons[removeButtons.length - 1]);

    await vi.waitFor(() =>
      expect(unlinkSourceMock).toHaveBeenCalledWith(
        { kbId: "kb-1", sourceId: "ds-linked" },
        expect.anything(),
      ),
    );
  });

  it("adding an unlinked source calls sync with this base's id", () => {
    renderWithClient(<KbDetailDrawer kbId="kb-1" onClose={vi.fn()} onSync={vi.fn()} />);

    fireEvent.change(screen.getByDisplayValue(/add existing source/i), {
      target: { value: "ds-unlinked" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));

    expect(syncSourceMock).toHaveBeenCalledWith(
      { sourceId: "ds-unlinked", kbId: "kb-1" },
      expect.anything(),
    );
  });

  it("does not offer an already-linked source in the add picker", () => {
    renderWithClient(<KbDetailDrawer kbId="kb-1" onClose={vi.fn()} onSync={vi.fn()} />);
    const picker = screen.getByDisplayValue(/add existing source/i);
    expect(screen.queryByText("Marketing site", { selector: "option" })).not.toBeInTheDocument();
    expect(picker).toBeInTheDocument();
  });
});

// Regression for "Man müsste das embedding auf dem modal beim edit von
// knowledgebase wechseln": the embedding model can only be changed while the
// base has never ingested a chunk (kb.chunks === 0) -- otherwise old and new
// chunks would sit in the same base embedded by different, incomparable
// models. This is also the exact fix path for the two pre-existing bases
// created with the now-removed bge-large option (see BaseWizard).
describe("KbDetailDrawer embedding model", () => {
  beforeEach(() => {
    updateKnowledgeBaseMock.mockReset();
  });

  it("locks the embedding model once the base has ingested content", () => {
    knowledgeBasesMock.mockReturnValue({ data: { items: [salesKb], totalCount: 1 } });
    renderWithClient(<KbDetailDrawer kbId="kb-1" onClose={vi.fn()} onSync={vi.fn()} />);

    expect(screen.getByText("google/gemini-embedding-2")).toBeInTheDocument();
    expect(screen.queryByTitle(/embedding model/i)).not.toBeInTheDocument();
  });

  it("offers an editable embedding model while the base is still empty", () => {
    knowledgeBasesMock.mockReturnValue({ data: { items: [emptyKb], totalCount: 1 } });
    renderWithClient(<KbDetailDrawer kbId="kb-empty" onClose={vi.fn()} onSync={vi.fn()} />);

    const select = screen.getByTitle(/embedding model/i) as HTMLSelectElement;
    expect(select.value).toBe("local/bge-large");

    fireEvent.change(select, { target: { value: "local/nomic-embed-text" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(updateKnowledgeBaseMock).toHaveBeenCalledWith(
      {
        kbId: "kb-empty",
        name: "Empty KB",
        description: "Pricing, playbooks, objection handling.",
        embeddingModel: "local/nomic-embed-text",
      },
      expect.anything(),
    );
  });
});

// Regression for "Ich kann keine Knowledgebases löschen": DELETE
// /knowledge/bases/{id} already existed on the backend (tombstone_base) but
// had no frontend hook or button anywhere in the UI.
describe("KbDetailDrawer delete", () => {
  beforeEach(() => {
    deleteKnowledgeBaseMock.mockReset();
    knowledgeBasesMock.mockReturnValue({ data: { items: [salesKb], totalCount: 1 } });
  });

  it("does not delete when the confirmation is dismissed", async () => {
    renderWithClient(<KbDetailDrawer kbId="kb-1" onClose={vi.fn()} onSync={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    // Flush the confirm dialog's resolved promise before asserting a
    // negative -- there is nothing observable to `waitFor` on here.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(deleteKnowledgeBaseMock).not.toHaveBeenCalled();
  });

  it("deletes the base once the confirmation is accepted, and closes the drawer", async () => {
    const onClose = vi.fn();
    renderWithClient(<KbDetailDrawer kbId="kb-1" onClose={onClose} onSync={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    // The confirm dialog's own action button shares the "Delete" label with
    // the row's trigger button, so two matches exist once it's open.
    const deleteButtons = screen.getAllByRole("button", { name: /^delete$/i });
    fireEvent.click(deleteButtons[deleteButtons.length - 1]);

    await vi.waitFor(() =>
      expect(deleteKnowledgeBaseMock).toHaveBeenCalledWith("kb-1", expect.anything()),
    );
    const [, opts] = deleteKnowledgeBaseMock.mock.calls[0];
    opts.onSuccess();
    expect(onClose).toHaveBeenCalled();
  });
});

// Regression coverage for the reported bug: the embedding-model picker
// offered google/openai options even though this deployment can never
// route embeddings to a cloud provider (ModelRouter.embed() always uses
// the local Ollama instance) -- and the wizard offered no way to add
// sources at creation time, only afterward.
describe("BaseWizard", () => {
  beforeEach(() => {
    createKnowledgeBaseMock.mockReset();
    syncSourceMock.mockReset();
  });

  function goToStep(n: number) {
    fireEvent.change(screen.getByPlaceholderText(/sales kb/i), { target: { value: "New KB" } });
    for (let i = 1; i < n; i++) {
      fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    }
  }

  it("offers only on-prem embedding models that fit this deployment's vector width, never a cloud provider or a mismatched dimension", () => {
    renderWithClient(<BaseWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    goToStep(3);

    const options = screen.getAllByRole("option") as HTMLOptionElement[];
    const values = options.map((o) => o.value);
    // bge-large produces 1024-dim vectors; this deployment's kb_chunk.embedding
    // column is a fixed 768-dim Vector(EMBED_DIM) -- offering it here would let
    // an operator create a base every future document sync fails to ingest into.
    expect(values).toEqual(["local/nomic-embed-text"]);
    expect(values.some((v) => v.includes("bge-large"))).toBe(false);
    expect(values.some((v) => v.includes("google") || v.includes("openai"))).toBe(false);
  });

  it("syncs each selected source into the newly created base", () => {
    createKnowledgeBaseMock.mockImplementation(
      (_vars: unknown, opts: { onSuccess: (kb: { id: string }) => void }) =>
        opts.onSuccess({ id: "new-kb-id" }),
    );
    renderWithClient(<BaseWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    goToStep(2);

    // Both fixture sources are `connected: true`, so both are offered here --
    // pick the one this test asserts on specifically.
    fireEvent.click(
      screen.getByText("Marketing site").closest("label")!.querySelector("input[type=checkbox]")!,
    );
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.click(screen.getByRole("button", { name: /create knowledge base/i }));

    expect(syncSourceMock).toHaveBeenCalledWith(
      { sourceId: "ds-linked", kbId: "new-kb-id" },
      expect.anything(),
    );
  });

  it("creates with no sources selected without calling sync at all", () => {
    createKnowledgeBaseMock.mockImplementation(
      (_vars: unknown, opts: { onSuccess: (kb: { id: string }) => void }) =>
        opts.onSuccess({ id: "new-kb-id" }),
    );
    renderWithClient(<BaseWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    goToStep(3);

    fireEvent.click(screen.getByRole("button", { name: /create knowledge base/i }));

    expect(syncSourceMock).not.toHaveBeenCalled();
  });
});

// Regression: BasesTab's card read `kb.embeddingModel.split("/")[1]` --
// correct for the `local/bge-large` convention BaseWizard writes, but a
// bare value like "nomic-embed-text" (this codebase's own server-side
// default, and what pre-existing seeded KBs actually carry) has no "/" at
// all, so `split("/")[1]` was `undefined` and the Model field rendered
// completely blank.
describe("BasesTab Model display", () => {
  it("shows the model as-is when it has no local/ prefix", () => {
    knowledgeBasesMock.mockReturnValue({
      data: {
        items: [
          { ...salesKb, id: "kb-bare", name: "Bare Model KB", embeddingModel: "nomic-embed-text" },
        ],
        totalCount: 1,
      },
    });
    renderWithClient(
      <BasesTab
        bases={[
          { ...salesKb, id: "kb-bare", name: "Bare Model KB", embeddingModel: "nomic-embed-text" },
        ]}
        onOpen={vi.fn()}
        onAdd={vi.fn()}
        queryState={{
          search: "",
          filters: {},
          groupBy: null,
          includeArchived: false,
          page: 1,
          pageSize: 20,
        }}
        onQueryStateChange={vi.fn()}
        totalCount={1}
      />,
    );

    expect(screen.getByText("nomic-embed-text")).toBeInTheDocument();
  });

  it("still splits off the local/ prefix for the normal convention", () => {
    renderWithClient(
      <BasesTab
        bases={[{ ...salesKb, embeddingModel: "local/bge-large" }]}
        onOpen={vi.fn()}
        onAdd={vi.fn()}
        queryState={{
          search: "",
          filters: {},
          groupBy: null,
          includeArchived: false,
          page: 1,
          pageSize: 20,
        }}
        onQueryStateChange={vi.fn()}
        totalCount={1}
      />,
    );

    expect(screen.getByText("bge-large")).toBeInTheDocument();
    expect(screen.queryByText("local/bge-large")).not.toBeInTheDocument();
  });
});

// Regression for the reported "beim Anlegen einer Datasource kann ich kein
// Embedding-Modell auswählen": SourceWizard's inline "no knowledge base yet
// -- name one now" prompt (step 3, when the tenant has zero KBs) created one
// via useCreateKnowledgeBase with ONLY a name -- no embedding-model control
// existed at all, so it silently took the server's bare default with no
// user choice, exactly like BaseWizard did before its own fix.
describe("SourceWizard inline KB creation", () => {
  beforeEach(() => {
    createKnowledgeBaseMock.mockReset();
    knowledgeBasesMock.mockReturnValue({ data: { items: [], totalCount: 0 } });
  });

  function goToStep3() {
    // Step 1: a connector kind is auto-selected by the wizard's own effect;
    // just advance.
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    // Step 2: connector config -- no required fields in this test's stub
    // schema, just advance.
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  }

  it("offers an embedding-model choice when creating the inline knowledge base", () => {
    renderWithClient(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    goToStep3();

    const options = screen.getAllByRole("option") as HTMLOptionElement[];
    const values = options.map((o) => o.value);
    // bge-large produces 1024-dim vectors against this deployment's fixed
    // 768-dim kb_chunk.embedding column -- only a model whose width actually
    // fits belongs in this list (see BaseWizard's identical constraint).
    expect(values).toEqual(["local/nomic-embed-text"]);
  });

  it("passes the chosen embedding model when creating the inline knowledge base", () => {
    renderWithClient(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    goToStep3();

    fireEvent.change(screen.getByPlaceholderText(/company knowledge base/i), {
      target: { value: "My New KB" },
    });
    fireEvent.change(screen.getByTitle(/embedding model/i), {
      target: { value: "local/nomic-embed-text" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));

    expect(createKnowledgeBaseMock).toHaveBeenCalledWith(
      { name: "My New KB", embeddingModel: "local/nomic-embed-text" },
      expect.anything(),
    );
  });
});

// Regression for "Aber das ist ja als flow nicht wirklich schön... es sollte
// bei connect dann automatisch das secret angelegt werden", now served by the
// unified credentials framework (design, Task 12): S3's `credential` field is
// marked `credentialType: "s3_api"` in the connector's schema and renders as
// a <CredentialPicker> -- pick an EXISTING credential (no keys re-entered at
// all, the original complaint) or create one inline. Either way the id it
// produces flows straight into `config.credential`; the wizard performs no
// secret-provisioning of its own any more.
describe("SourceWizard credential-backed connector fields", () => {
  beforeEach(() => {
    createSourceMock.mockReset();
    knowledgeConnectorsMock.mockReturnValue(S3_CONNECTORS_RESPONSE);
  });

  function fillAndSubmitS3Source() {
    // Step 1: s3 is auto-selected (only connector in this stub list).
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    // Step 2: connector config -- bucket, plus picking the EXISTING
    // credential from the dropdown (never "Create new").
    fireEvent.change(screen.getByLabelText("Bucket", { exact: false }), {
      target: { value: "my-bucket" },
    });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    // Step 3: sync schedule -- default is fine, a KB already exists (salesKb).
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    // Step 4: sensitivity -- default is fine.
    fireEvent.click(screen.getByRole("button", { name: /^connect$/i }));
  }

  it("renders the credential field via CredentialPicker, not a raw password input", () => {
    renderWithClient(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(screen.getByLabelText("Bucket", { exact: false })).toHaveAttribute("type", "text");
    expect(screen.getByText("Prod S3", { selector: "option" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create new/i })).toBeInTheDocument();
  });

  it("submits the selected EXISTING credential's id as config.credential -- no keys re-entered", async () => {
    renderWithClient(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fillAndSubmitS3Source();

    await vi.waitFor(() => expect(createSourceMock).toHaveBeenCalled());

    // The picker's own credential-creation flow must never have been used.
    expect(createCredentialMock).not.toHaveBeenCalled();

    const [sourceBody] = createSourceMock.mock.calls[0];
    expect(sourceBody.config.bucket).toBe("my-bucket");
    expect(sourceBody.config.credential).toBe("cred-1");
  });

  it("wires a newly created credential's id into config.credential when 'Create new' is used", async () => {
    renderWithClient(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.change(screen.getByLabelText("Bucket", { exact: false }), {
      target: { value: "my-bucket" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create new/i }));
    // Exact match (not `{ exact: false }`): the field's own inner <label>
    // matches "Access key" exactly, but the DynamicConnectorFields wrapper
    // ALSO puts an outer <label> around the whole CredentialPicker
    // subtree (matching PluginSetupFields' identical pattern) -- with
    // `exact: false`'s substring matching, that outer label's aggregated
    // text (every nested field's label concatenated) ALSO contains "Access
    // key", so the query becomes ambiguous.
    fireEvent.change(screen.getByLabelText("Access key"), {
      target: { value: "AKIA_X" },
    });
    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: "New S3 credential" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await vi.waitFor(() => expect(createCredentialMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^connect$/i }));

    await vi.waitFor(() => expect(createSourceMock).toHaveBeenCalled());
    const [sourceBody] = createSourceMock.mock.calls[0];
    expect(sourceBody.config.credential).toBe("new-cred-id");
  });
});

// Regression for "Dann sollte aber die UI den Fehler auch irgendwie besser
// anzeigen": a validation rejection (e.g. the S3 "prefix is the same as
// the bucket name" check) used to surface only as a toast, easy to miss
// and gone within seconds. It now also renders as a banner inside the
// wizard itself that stays until the user edits a field and retries.
describe("SourceWizard persistent error banner", () => {
  beforeEach(() => {
    createSourceMock.mockReset();
    knowledgeConnectorsMock.mockReturnValue(S3_CONNECTORS_RESPONSE);
  });

  function fillS3Fields() {
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.change(screen.getByLabelText("Bucket", { exact: false }), {
      target: { value: "oc8-test2" },
    });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  }

  it("keeps the rejection message visible in the wizard, not just as a toast", async () => {
    const rejection = "prefix 'oc8-test2' is the same as the bucket name -- ...";
    createSourceMock.mockImplementation((_body: unknown, opts: { onError: (e: Error) => void }) =>
      opts.onError(new Error(rejection)),
    );
    renderWithClient(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fillS3Fields();
    fireEvent.click(screen.getByRole("button", { name: /^connect$/i }));

    await vi.waitFor(() => expect(screen.getByText(rejection)).toBeInTheDocument());
  });

  it("clears the banner once the user edits a field again", async () => {
    const rejection = "prefix 'oc8-test2' is the same as the bucket name -- ...";
    createSourceMock.mockImplementation((_body: unknown, opts: { onError: (e: Error) => void }) =>
      opts.onError(new Error(rejection)),
    );
    renderWithClient(<SourceWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fillS3Fields();
    fireEvent.click(screen.getByRole("button", { name: /^connect$/i }));
    await vi.waitFor(() => expect(createSourceMock).toHaveBeenCalled());
    await vi.waitFor(() => expect(screen.getByText(rejection)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /^back$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^back$/i }));
    // A DIFFERENT value than what's already there -- React's input value
    // tracker skips firing onChange when fireEvent.change sets the exact
    // same string a controlled input already holds.
    fireEvent.change(screen.getByLabelText("Bucket", { exact: false }), {
      target: { value: "oc8-test2/reports" },
    });

    expect(screen.queryByText(rejection)).not.toBeInTheDocument();
  });
});

const QDRANT_INDEX = {
  typeId: "qdrant",
  label: "Qdrant",
  description: "Search an existing Qdrant collection",
  credentialType: "qdrant_api",
  configSchema: {
    properties: {
      collection: { type: "string", title: "Collection" },
    },
    required: ["collection"],
  },
};

const QDRANT_CREDENTIAL_TYPE = {
  name: "qdrant_api",
  displayName: "Qdrant",
  fields: [
    {
      key: "url",
      label: "URL",
      kind: "url",
      required: true,
      default: "",
      placeholder: "",
      help: "",
    },
    {
      key: "api_key",
      label: "API key",
      kind: "password",
      required: false,
      default: "",
      placeholder: "",
      help: "",
    },
  ],
};

const QDRANT_CREDENTIAL = { id: "qdrant-cred-1", name: "Prod Qdrant", credentialType: "qdrant_api" };

describe("BaseWizard connect existing vector index", () => {
  beforeEach(() => {
    createKnowledgeBaseMock.mockReset();
    knowledgeVectorIndexesMock.mockReturnValue({ data: [QDRANT_INDEX], isLoading: false });
    credentialsMock.mockImplementation((credentialType?: string) => ({
      data:
        !credentialType || credentialType === "qdrant_api" ? [QDRANT_CREDENTIAL] : [EXISTING_CREDENTIAL],
    }));
    credentialTypesMock.mockReturnValue({ data: [QDRANT_CREDENTIAL_TYPE, S3_CREDENTIAL_TYPE] });
  });

  it("renders CredentialPicker for the index credential, not a raw password input", () => {
    renderWithClient(<BaseWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText(/sales kb/i), { target: { value: "HR Index" } });
    fireEvent.click(screen.getByLabelText(/connect existing vector index/i));
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.click(screen.getByRole("button", { name: /qdrant/i }));

    expect(screen.getByText("Prod Qdrant", { selector: "option" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create new/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/^api key$/i)).not.toBeInTheDocument();
  });

  it("submits indexType, indexConfig, and credentialId — no secrets in the body", async () => {
    renderWithClient(<BaseWizard onClose={vi.fn()} onCreate={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText(/sales kb/i), { target: { value: "HR Index" } });
    fireEvent.click(screen.getByLabelText(/connect existing vector index/i));
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.click(screen.getByRole("button", { name: /qdrant/i }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "qdrant-cred-1" } });
    fireEvent.change(screen.getByLabelText(/collection/i), { target: { value: "hr_docs" } });
    fireEvent.click(screen.getByRole("button", { name: /^continue$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^connect index$/i }));

    await vi.waitFor(() => expect(createKnowledgeBaseMock).toHaveBeenCalled());
    const [body] = createKnowledgeBaseMock.mock.calls[0];
    expect(body).toEqual(
      expect.objectContaining({
        name: "HR Index",
        indexType: "qdrant",
        credentialId: "qdrant-cred-1",
        indexConfig: expect.objectContaining({ collection: "hr_docs" }),
      }),
    );
    expect(JSON.stringify(body)).not.toMatch(/api[_-]?key/i);
    expect(JSON.stringify(body)).not.toMatch(/password/i);
  });
});
