import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FileCode2 } from "lucide-react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CustomMcpWizard } from "@/components/custom-mcp-wizard";
import { EditionCompositionProvider, composeFrontendEdition } from "@/edition/composition";
import type {
  CustomServerDraft,
  CustomServerSourceProps,
  FrontendEditionExtension,
} from "@/edition/contracts";

const { postMock, getMock, patchMock, deleteMock } = vi.hoisted(() => ({
  postMock: vi.fn(),
  getMock: vi.fn(),
  patchMock: vi.fn(),
  deleteMock: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  api: { post: postMock, get: getMock, patch: patchMock, delete: deleteMock },
}));

const DRAFT: CustomServerDraft = {
  baseUrl: "https://api.acme.test/v1",
  login: { kind: "api_key", in: "header", name: "X-Api-Key", secret: "k-9" },
  httpTools: [
    {
      name: "list_pets",
      description: "List pets.",
      method: "GET",
      urlTemplate: "/pets",
      paramSchema: { type: "object", properties: { limit: { type: "integer" } } },
      paramLocations: { limit: "query" },
      bodyParam: null,
      group: "pets",
    },
  ],
  scopes: { read: ["list_pets"], modify: [] },
  openapi: {
    sourceUrl: "https://api.acme.test/openapi.json",
    fingerprint: "f1",
    importedAt: "2026-11-13T10:00:00Z",
    title: "Pets",
    version: "1.0",
  },
};

const BAD_DRAFTS: Array<[string, CustomServerDraft]> = [
  ["Use read and modify", { ...DRAFT, scopes: { read: ["list_pets"], modify: ["list_pets"] } }],
  ["Use an unknown scope", { ...DRAFT, scopes: { read: ["list_pets", "drop_pets"], modify: [] } }],
  ["Use no base URL", { ...DRAFT, baseUrl: "  " }],
  [
    "Use no secret",
    { ...DRAFT, login: { kind: "api_key", in: "header", name: "X-Api-Key", secret: "" } },
  ],
];

function FakeSource({ mode, onComplete }: CustomServerSourceProps) {
  return (
    <div>
      <p>fake source in {mode} mode</p>
      <button type="button" onClick={() => onComplete(DRAFT)}>
        Use this API
      </button>
      <button
        type="button"
        onClick={() =>
          onComplete({ ...DRAFT, httpTools: [{ ...DRAFT.httpTools[0], name: "list pets" }] })
        }
      >
        Use a bad name
      </button>
      {BAD_DRAFTS.map(([label, draft]) => (
        <button key={label} type="button" onClick={() => onComplete(draft)}>
          {label}
        </button>
      ))}
      <button
        type="button"
        onClick={() =>
          onComplete({
            ...DRAFT,
            httpTools: [DRAFT.httpTools[0], { ...DRAFT.httpTools[0], name: "count_pets" }],
            scopes: { read: ["list_pets", "count_pets"], modify: [] },
          })
        }
      >
        Use more endpoints
      </button>
      <button
        type="button"
        onClick={() => onComplete({ ...DRAFT, baseUrl: "https://api.acme.test/v2" })}
      >
        Use another server
      </button>
    </div>
  );
}

const ext: FrontendEditionExtension = {
  id: "test-edition",
  customServerSources: [
    {
      id: "spec",
      label: { en: "From a spec file", de: "Aus einer Spec-Datei" },
      description: {
        en: "Read the endpoints from a file.",
        de: "Liest die Endpunkte aus einer Datei.",
      },
      icon: FileCode2,
      component: FakeSource,
    },
  ],
};

function wrap(extensions: FrontendEditionExtension[]) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={new QueryClient()}>
        <EditionCompositionProvider extensions={extensions}>{children}</EditionCompositionProvider>
      </QueryClientProvider>
    );
  };
}

describe("customServerSources", () => {
  beforeEach(() => {
    postMock.mockReset();
    getMock.mockReset();
    patchMock.mockReset();
    deleteMock.mockReset();
    getMock.mockResolvedValue({ items: [], totalCount: 0 });
  });

  it("composes sources and refuses a duplicate id", () => {
    expect(composeFrontendEdition([ext]).customServerSources.map((s) => s.id)).toEqual(["spec"]);
    expect(() => composeFrontendEdition([ext, { ...ext, id: "other" }])).toThrow(
      "Duplicate custom server source: spec",
    );
  });

  it("offers a registered source above describing by hand", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper: wrap([ext]) });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    const source = screen.getByRole("button", { name: /From a spec file/ });
    const byHand = screen.getByRole("button", { name: /Describe them by hand/ });
    expect(source.compareDocumentPosition(byHand) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows no chooser without a registered source", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper: wrap([]) });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    expect(screen.queryByText(/Describe them by hand/)).not.toBeInTheDocument();
  });

  it("hides a source whose capability the caller lacks", async () => {
    getMock.mockImplementation((url: string) =>
      Promise.resolve(
        url.startsWith("/governance") ? { callerCapabilities: [] } : { items: [], totalCount: 0 },
      ),
    );
    const gated = {
      ...ext,
      customServerSources: [
        { ...ext.customServerSources![0], needs: ["enterprise.openapi_import"] },
      ],
    };
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper: wrap([gated]) });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    await waitFor(() => expect(screen.queryByText("From a spec file")).not.toBeInTheDocument());
  });

  it("installs what the source handed back", async () => {
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") return Promise.resolve({ id: "cv-1", pluginId: "capa-1" });
      if (path === "/capas/capa-1/enable") return Promise.resolve({});
      if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper: wrap([ext]) });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Pets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /From a spec file/ }));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));

    expect(screen.getByText("fake source in create mode")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /next/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use this API" }));

    expect(await screen.findByRole("button", { name: "Run test call" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/capas/capa-1/setup", {
        values: { auth_secret: "k-9" },
      }),
    );
    const manifest = postMock.mock.calls.find(([path]) => path === "/capas")?.[1].manifest;
    const conn = manifest.tool_pack.connections[0];
    expect(conn.server_url).toBe("https://api.acme.test/v1");
    expect(conn.transport).toBe("manual_http");
    expect(conn.scopes).toEqual({ read: ["list_pets"], modify: [] });
    expect(conn.config.auth).toEqual({ kind: "api_key", in: "header", name: "X-Api-Key" });
    expect(conn.config.http_tools).toEqual([
      {
        name: "list_pets",
        description: "List pets.",
        method: "GET",
        url_template: "/pets",
        param_schema: { type: "object", properties: { limit: { type: "integer" } } },
        param_locations: { limit: "query" },
        body_param: null,
        group: "pets",
      },
    ]);
    expect(conn.config.openapi).toEqual({
      source_url: "https://api.acme.test/openapi.json",
      fingerprint: "f1",
      imported_at: "2026-11-13T10:00:00Z",
      title: "Pets",
      version: "1.0",
    });
    expect(manifest.setup.mcp.secret_env_fields).toEqual({ HTTP_AUTH_SECRET: "auth_secret" });
  });

  it("keeps the source step open when what it handed back would be refused", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper: wrap([ext]) });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Pets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /From a spec file/ }));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Use a bad name" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/list pets: Use only letters/);
    expect(screen.getByText("fake source in create mode")).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
  });

  it.each([
    ["Use read and modify", /either reads or modifies, not both: list_pets/],
    ["Use an unknown scope", /not there: drop_pets/],
    ["Use no base URL", /base URL/],
    ["Use no secret", /login is incomplete/],
  ])("keeps the source step open for %s", (label, message) => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper: wrap([ext]) });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Pets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /From a spec file/ }));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(screen.getByText("fake source in create mode")).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
  });

  describe("a draft that changes after a test call", () => {
    beforeEach(() => {
      postMock.mockImplementation((path: string) => {
        if (path === "/capas") return Promise.resolve({ id: "cv-1", pluginId: "capa-1" });
        if (path === "/capas/capa-1/enable") return Promise.resolve({});
        if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
        if (path === "/mcp/connections/conn-1/test-call") {
          return Promise.resolve({
            ok: true,
            status: 200,
            durationMs: 5,
            excerpt: "[]",
            truncated: false,
            errorClass: null,
            message: "The API answered 200.",
          });
        }
        return Promise.reject(new Error(`unexpected POST ${path}`));
      });
      patchMock.mockResolvedValue({});
      deleteMock.mockResolvedValue(undefined);
    });

    async function testThenHandBack(label: string) {
      render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper: wrap([ext]) });
      fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Pets" } });
      fireEvent.click(screen.getByText("Plain HTTP API"));
      fireEvent.click(screen.getByRole("button", { name: /From a spec file/ }));
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: "Use this API" }));
      fireEvent.click(await screen.findByRole("button", { name: "Run test call" }));
      await screen.findByText("The call worked.");
      fireEvent.click(screen.getByRole("button", { name: /back/i }));
      fireEvent.click(screen.getByRole("button", { name: label }));
      await screen.findByRole("button", { name: "Run test call" });
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    }

    it("replaces only the tools when only the endpoints changed", async () => {
      await testThenHandBack("Use more endpoints");
      await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
      const [path, body] = patchMock.mock.calls[0];
      expect(path).toBe("/mcp/connections/conn-1/http-tools");
      expect(body.httpTools.map((tool: { name: string }) => tool.name)).toEqual([
        "list_pets",
        "count_pets",
      ]);
      expect(body.httpTools[1]).toMatchObject({ urlTemplate: "/pets", group: "pets" });
      expect(body.scopes).toEqual({ read: ["list_pets", "count_pets"], modify: [] });
      expect(body.openapi).toEqual(DRAFT.openapi);
      await waitFor(() =>
        expect(postMock.mock.calls.filter(([path]) => path === "/capas/capa-1/setup")).toHaveLength(
          1,
        ),
      );
      expect(postMock.mock.calls.filter(([path]) => path === "/capas")).toHaveLength(1);
      expect(deleteMock).not.toHaveBeenCalled();
    });

    it("installs again when the server changed", async () => {
      await testThenHandBack("Use another server");
      await waitFor(() =>
        expect(postMock.mock.calls.filter(([path]) => path === "/capas")).toHaveLength(2),
      );
      const second = postMock.mock.calls.filter(([path]) => path === "/capas")[1][1].manifest;
      expect(second.tool_pack.connections[0].server_url).toBe("https://api.acme.test/v2");
      expect(patchMock).not.toHaveBeenCalled();
      expect(deleteMock).toHaveBeenCalledWith("/mcp/connections/conn-1");
    });
  });
});
