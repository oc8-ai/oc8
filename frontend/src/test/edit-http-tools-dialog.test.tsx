import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FileCode2 } from "lucide-react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpConnectionBody } from "@/components/mcp-connections";
import { EditionCompositionProvider } from "@/edition/composition";
import type { CustomServerSourceProps, FrontendEditionExtension } from "@/edition/contracts";
import type { McpConnection } from "@/lib/hooks";

const { getMock, patchMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  patchMock: vi.fn(),
  postMock: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  api: { get: getMock, post: postMock, patch: patchMock, delete: vi.fn() },
}));

const SNAPSHOT = {
  connectionId: "conn-1",
  name: "pets",
  baseUrl: "https://api.acme.test",
  httpTools: [
    {
      name: "list_pets",
      description: "List pets.",
      method: "GET",
      urlTemplate: "/pets",
      paramSchema: {},
    },
  ],
  scopes: { read: ["list_pets"], modify: [] },
  auth: null,
  openapi: {
    sourceUrl: null,
    fingerprint: "f1",
    importedAt: "2026-11-13T10:00:00Z",
    title: "Pets",
    version: "1",
  },
};

function EditingSource({ mode, existing, onComplete }: CustomServerSourceProps) {
  return (
    <div>
      <p>
        editing {existing?.name} in {mode} mode
      </p>
      <button
        type="button"
        onClick={() =>
          onComplete({
            baseUrl: "",
            login: { kind: "none" },
            httpTools: existing!.httpTools,
            scopes: { read: [], modify: ["list_pets"] },
            openapi: existing!.openapi,
          })
        }
      >
        Save endpoints
      </button>
      <button
        type="button"
        onClick={() =>
          onComplete({
            baseUrl: "",
            login: { kind: "none" },
            httpTools: [{ ...existing!.httpTools[0], urlTemplate: "https://elsewhere.test/pets" }],
            scopes: { read: ["list_pets"], modify: [] },
            openapi: existing!.openapi,
          })
        }
      >
        Save a full URL
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
      description: { en: "x", de: "x" },
      icon: FileCode2,
      component: EditingSource,
      canEdit: (existing) => existing.openapi !== null,
    },
  ],
};

function connection(patch: Partial<McpConnection> = {}): McpConnection {
  return {
    id: "conn-1",
    name: "pets",
    transport: "manual_http",
    serverUrl: "https://api.acme.test",
    command: "",
    args: [],
    departmentId: "d-1",
    needsDepartment: false,
    connected: true,
    scopes: [],
    health: { status: "ok", tools: ["list_pets"] },
    guardrailPresets: [],
    guardrailLibrary: null,
    guardrailAttributes: [],
    hasValueSpec: false,
    ...patch,
  } as McpConnection;
}

function wrap(extensions: FrontendEditionExtension[]) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={new QueryClient()}>
        <EditionCompositionProvider extensions={extensions}>{children}</EditionCompositionProvider>
      </QueryClientProvider>
    );
  };
}

function grant(permissions: string[]) {
  getMock.mockImplementation((url: string) => {
    if (url.startsWith("/governance")) return Promise.resolve({ callerPermissions: permissions });
    if (url === "/mcp/connections/conn-1/http-tools") return Promise.resolve(SNAPSHOT);
    return Promise.resolve({ items: [], totalCount: 0 });
  });
}

describe("Edit endpoints", () => {
  beforeEach(() => {
    getMock.mockReset();
    patchMock.mockReset();
    postMock.mockReset();
  });

  it("is offered when a source can edit the connection and the caller may manage plugins", async () => {
    grant(["plugin:manage"]);
    render(<McpConnectionBody connection={connection()} />, { wrapper: wrap([ext]) });
    expect(await screen.findByRole("button", { name: "Edit endpoints" })).toBeInTheDocument();
  });

  it("is not offered without an editing source", async () => {
    grant(["plugin:manage"]);
    render(<McpConnectionBody connection={connection()} />, { wrapper: wrap([]) });
    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Edit endpoints" })).not.toBeInTheDocument();
    expect(getMock).not.toHaveBeenCalledWith("/mcp/connections/conn-1/http-tools");
  });

  it("is not offered to a caller who may not manage plugins", async () => {
    grant([]);
    render(<McpConnectionBody connection={connection()} />, { wrapper: wrap([ext]) });
    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Edit endpoints" })).not.toBeInTheDocument();
  });

  it("saves through PATCH and then offers a test call", async () => {
    grant(["plugin:manage"]);
    patchMock.mockResolvedValue({ ...SNAPSHOT, scopes: { read: [], modify: ["list_pets"] } });
    render(<McpConnectionBody connection={connection()} />, { wrapper: wrap([ext]) });
    fireEvent.click(await screen.findByRole("button", { name: "Edit endpoints" }));
    expect(screen.getByText("editing pets in edit mode")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save endpoints" }));
    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith("/mcp/connections/conn-1/http-tools", {
        httpTools: SNAPSHOT.httpTools,
        scopes: { read: [], modify: ["list_pets"] },
        openapi: SNAPSHOT.openapi,
      }),
    );
    expect(await screen.findByRole("button", { name: "Run test call" })).toBeInTheDocument();
  });

  it("refuses an endpoint the server would refuse, without saving", async () => {
    grant(["plugin:manage"]);
    render(<McpConnectionBody connection={connection()} />, { wrapper: wrap([ext]) });
    fireEvent.click(await screen.findByRole("button", { name: "Edit endpoints" }));
    fireEvent.click(screen.getByRole("button", { name: "Save a full URL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "list_pets: Enter the path below the base URL, starting with /.",
    );
    expect(patchMock).not.toHaveBeenCalled();
  });
});
