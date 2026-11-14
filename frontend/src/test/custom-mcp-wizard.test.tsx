import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CustomMcpWizard } from "@/components/custom-mcp-wizard";

const { postMock, getMock, deleteMock, patchMock, toastWarning } = vi.hoisted(() => ({
  postMock: vi.fn(),
  getMock: vi.fn(),
  deleteMock: vi.fn(),
  patchMock: vi.fn(),
  toastWarning: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: toastWarning },
}));

vi.mock("@/lib/api", () => ({
  api: {
    post: postMock,
    get: getMock,
    patch: patchMock,
    delete: deleteMock,
  },
}));

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient();
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

// Drives the wizard through Name/Type -> Connection (stdio) -> a successful
// "Test connection", the same real install+enable+setup sequence step 3 runs
// against the (mocked) backend -- so `state.installedPluginId` is set the
// way it would be after a real test, before the test exercises cancelling.
async function installAndTestAStdioServer() {
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My server" } });
  fireEvent.click(screen.getByText("Local MCP server"));
  fireEvent.click(screen.getByRole("button", { name: /next/i }));

  fireEvent.change(screen.getByLabelText("Command"), { target: { value: "npx my-server" } });
  fireEvent.click(screen.getByRole("button", { name: /next/i }));

  fireEvent.click(screen.getByRole("button", { name: /^test connection$/i }));
  await screen.findByText(/Connected/i);
}

// Adds one tool with a name and a path below the base URL, the least the
// tools step lets through.
function addTool(name = "list_tickets", path = "/tickets") {
  fireEvent.click(screen.getByRole("button", { name: /add tool/i }));
  const names = screen.getAllByPlaceholderText("Tool name");
  fireEvent.change(names[names.length - 1], { target: { value: name } });
  const paths = screen.getAllByPlaceholderText("/orders/{order_id}");
  fireEvent.change(paths[paths.length - 1], { target: { value: path } });
}

describe("CustomMcpWizard", () => {
  beforeEach(() => {
    postMock.mockReset();
    getMock.mockReset();
    deleteMock.mockReset();
    deleteMock.mockResolvedValue(undefined);
    patchMock.mockReset();
    toastWarning.mockReset();
    getMock.mockResolvedValue({ items: [], totalCount: 0 });
  });

  it("renders step 1 with the three server-type cards when open", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    expect(screen.getByText(/Local MCP server/i)).toBeInTheDocument();
    expect(screen.getByText(/Remote MCP server/i)).toBeInTheDocument();
    expect(screen.getByText(/Plain HTTP API/i)).toBeInTheDocument();
  });

  it("does not render anything when closed", () => {
    render(<CustomMcpWizard open={false} onOpenChange={() => {}} />, { wrapper });
    expect(screen.queryByText(/Local MCP server/i)).not.toBeInTheDocument();
  });

  it("blocks advancing from step 1 until a name and server type are chosen", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    const next = screen.getByRole("button", { name: /next/i });
    expect(next).toBeDisabled();
  });

  it("closes immediately, with no confirmation, when cancelling before anything was installed", () => {
    const onOpenChange = vi.fn();
    render(<CustomMcpWizard open onOpenChange={onOpenChange} />, { wrapper });

    fireEvent.click(screen.getByRole("button", { name: /close/i }));

    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(postMock).not.toHaveBeenCalled();
  });

  it("warns before cancelling after a real test already installed and enabled the capa, and disables it on confirm", async () => {
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") {
        return Promise.resolve({
          id: "cv-1",
          pluginId: "capa-1",
          name: "My server",
          type: "tool_pack",
          semver: "1.0.0",
          trustLevel: "unverified",
        });
      }
      if (path === "/capas/capa-1/enable") return Promise.resolve({});
      if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
      if (path === "/mcp/connections/conn-1/test") {
        return Promise.resolve({ id: "conn-1", connected: true, health: { tools: ["ping"] } });
      }
      if (path === "/capas/capa-1/disable") return Promise.resolve({});
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    const onOpenChange = vi.fn();
    render(<CustomMcpWizard open onOpenChange={onOpenChange} />, { wrapper });

    await installAndTestAStdioServer();

    fireEvent.click(screen.getByRole("button", { name: /close/i }));

    // Honest about what happened -- not just "are you sure?".
    expect(await screen.findByText(/already installed and enabled/i)).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /disable & cancel/i }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/capas/capa-1/disable", {
        reason: "Cancelled from the custom MCP wizard after a connection test",
      }),
    );
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("keeps the wizard open, and never closes, when the user chooses to keep editing instead", async () => {
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") {
        return Promise.resolve({
          id: "cv-1",
          pluginId: "capa-1",
          name: "My server",
          type: "tool_pack",
          semver: "1.0.0",
          trustLevel: "unverified",
        });
      }
      if (path === "/capas/capa-1/enable") return Promise.resolve({});
      if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
      if (path === "/mcp/connections/conn-1/test") {
        return Promise.resolve({ id: "conn-1", connected: true, health: { tools: ["ping"] } });
      }
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    const onOpenChange = vi.fn();
    render(<CustomMcpWizard open onOpenChange={onOpenChange} />, { wrapper });

    await installAndTestAStdioServer();

    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(await screen.findByText(/already installed and enabled/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /keep editing/i }));

    expect(onOpenChange).not.toHaveBeenCalled();
    expect(postMock).not.toHaveBeenCalledWith("/capas/capa-1/disable", expect.anything());
    // Still on the test step with its result intact -- "Keep editing" did not
    // reset the wizard the way a real close() would have.
    expect(screen.getByText(/Connected/i)).toBeInTheDocument();
  });

  it("stores an API-key-in-query login as config.auth plus one sealed secret field", async () => {
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") return Promise.resolve({ id: "cv-1", pluginId: "capa-1" });
      if (path === "/capas/capa-1/enable") return Promise.resolve({});
      if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));

    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    fireEvent.change(screen.getByLabelText("Login"), { target: { value: "api_key" } });
    fireEvent.change(screen.getByLabelText("Send the key in"), { target: { value: "query" } });
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "api_key" } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "k-1" } });
    addTool();
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/capas/capa-1/setup", {
        values: { auth_secret: "k-1" },
      }),
    );
    const manifest = postMock.mock.calls.find(([path]) => path === "/capas")?.[1].manifest;
    expect(manifest.tool_pack.connections[0].config.auth).toEqual({
      kind: "api_key",
      in: "query",
      name: "api_key",
    });
    expect(manifest.tool_pack.connections[0].config.auth_header_name).toBeUndefined();
    expect(manifest.setup.fields).toContainEqual(
      expect.objectContaining({ key: "auth_secret", kind: "password" }),
    );
    expect(manifest.setup.mcp.secret_env_fields).toEqual({ HTTP_AUTH_SECRET: "auth_secret" });
  });

  it("refuses a Basic username with a colon and says why", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add tool/i }));
    fireEvent.change(screen.getByLabelText("Login"), { target: { value: "basic" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "ada:lovelace" } });
    expect(screen.getByText("The username can't contain a colon.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "ada" } });
    expect(screen.queryByText("The username can't contain a colon.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /next/i })).toBeEnabled();
  });

  it("keeps Next disabled until an OAuth2 login has its token URL, client and secret", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add tool/i }));
    fireEvent.change(screen.getByLabelText("Login"), {
      target: { value: "oauth2_client_credentials" },
    });
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Token URL"), {
      target: { value: "https://auth.acme.test/token" },
    });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "cid" } });
    fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "cs" } });
    expect(screen.getByRole("button", { name: /next/i })).toBeEnabled();
  });

  it("tests one endpoint of a plain HTTP API with a real call", async () => {
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") return Promise.resolve({ id: "cv-1", pluginId: "capa-1" });
      if (path === "/capas/capa-1/enable") return Promise.resolve({});
      if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
      if (path === "/mcp/connections/conn-1/test-call") {
        return Promise.resolve({
          ok: true,
          status: 200,
          durationMs: 30,
          excerpt: "[]",
          truncated: false,
          errorClass: null,
          message: "The API answered 200.",
        });
      }
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    addTool("list_tickets");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));

    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/mcp/connections/conn-1/test-call", {
        tool: "list_tickets",
        arguments: {},
      }),
    );
    expect(await screen.findByText("The call worked.")).toBeInTheDocument();
  });

  it("installs the edited connection again when it changed after a test call", async () => {
    let setups = 0;
    let installCount = 0;
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") {
        installCount += 1;
        return Promise.resolve({ id: "cv-1", pluginId: "capa-1" });
      }
      if (path === "/capas/capa-1/enable") return Promise.resolve({});
      if (path === "/capas/capa-1/setup") {
        setups += 1;
        // One connection per install: a reinstall's enable makes a new one,
        // a setup of the same department finds it again.
        return Promise.resolve({ connectionId: `conn-${installCount}` });
      }
      if (path.endsWith("/test-call")) {
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
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    addTool("list_tickets");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    expect(await screen.findByText("The call worked.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    fireEvent.change(screen.getByPlaceholderText("Tool name"), {
      target: { value: "list_open_tickets" },
    });
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://v2.tickets.acme.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/mcp/connections/conn-2/test-call", {
        tool: "list_open_tickets",
        arguments: {},
      }),
    );
    // The stale connection goes, so enabling the new version materialises
    // a fresh one from the edited manifest.
    expect(deleteMock).toHaveBeenCalledWith("/mcp/connections/conn-1");
    const installs = postMock.mock.calls.filter(([path]) => path === "/capas");
    expect(installs).toHaveLength(2);
    const second = installs[1][1].manifest;
    expect(second.version).not.toBe(installs[0][1].manifest.version);
    expect(second.tool_pack.connections[0].server_url).toBe("https://v2.tickets.acme.test");
    expect(second.tool_pack.connections[0].config.http_tools[0].name).toBe("list_open_tickets");

    await screen.findByText("The call worked.");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => expect(setups).toBe(3));
    // Unchanged since the last test: Save keeps that install.
    expect(postMock.mock.calls.filter(([path]) => path === "/capas")).toHaveLength(2);
    expect(deleteMock).toHaveBeenCalledTimes(1);
  });

  it("disables the capa installed under the old name after a rename", async () => {
    let installs = 0;
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") {
        installs += 1;
        return Promise.resolve({ id: `cv-${installs}`, pluginId: `capa-${installs}` });
      }
      if (/^\/capas\/capa-\d\/(enable|disable)$/.test(path)) return Promise.resolve({});
      if (/^\/capas\/capa-\d\/setup$/.test(path)) {
        return Promise.resolve({ connectionId: `conn-${installs}` });
      }
      if (path.endsWith("/test-call")) {
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
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    addTool("list_tickets");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    await screen.findByText("The call worked.");

    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Helpdesk" } });
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/mcp/connections/conn-2/test-call", {
        tool: "list_tickets",
        arguments: {},
      }),
    );
    expect(postMock).toHaveBeenCalledWith("/capas/capa-1/disable", {
      reason: "Replaced in the custom MCP wizard after a rename",
    });
  });

  // Drives a plain HTTP API named "Tickets" with one tool through one
  // successful test call.
  async function testAPlainHttpApi() {
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    addTool("list_tickets");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    await screen.findByText("The call worked.");
  }

  const CALL_WORKED = {
    ok: true,
    status: 200,
    durationMs: 5,
    excerpt: "[]",
    truncated: false,
    errorClass: null,
    message: "The API answered 200.",
  };

  it("replaces only the tools of the tested connection when nothing else changed", async () => {
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") return Promise.resolve({ id: "cv-1", pluginId: "capa-1" });
      if (path === "/capas/capa-1/enable") return Promise.resolve({});
      if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
      if (path.endsWith("/test-call")) return Promise.resolve(CALL_WORKED);
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    patchMock.mockResolvedValue({});
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    await testAPlainHttpApi();
    const postsBefore = postMock.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    fireEvent.change(screen.getByPlaceholderText("Tool name"), {
      target: { value: "list_open_tickets" },
    });
    fireEvent.change(screen.getByPlaceholderText("/orders/{order_id}"), {
      target: { value: "/tickets?state=open" },
    });
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/mcp/connections/conn-1/test-call", {
        tool: "list_open_tickets",
        arguments: {},
      }),
    );
    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(patchMock).toHaveBeenCalledWith("/mcp/connections/conn-1/http-tools", {
      httpTools: [
        {
          name: "list_open_tickets",
          description: "",
          method: "GET",
          urlTemplate: "/tickets?state=open",
          paramSchema: {},
        },
      ],
      scopes: { read: [], modify: [] },
      openapi: null,
    });
    // The PATCH and the test call itself, nothing else: no new install, no
    // enable, no setup, and the connection stays.
    expect(postMock.mock.calls.slice(postsBefore).map(([path]) => path)).toEqual([
      "/mcp/connections/conn-1/test-call",
    ]);
    expect(deleteMock).not.toHaveBeenCalled();
  });

  it.each([
    ["", "/tickets", "", "Give this tool a name."],
    [
      "list tickets",
      "/tickets",
      "",
      "Use only letters, digits, _ and - in a tool name (at most 64).",
    ],
    ["list_tickets", "", "", "Enter the path below the base URL, starting with /."],
    [
      "list_tickets",
      "https://evil.test/x",
      "",
      "Enter the path below the base URL, starting with /.",
    ],
    ["list_tickets", "/tickets", "{", "The parameter schema is not valid JSON."],
    [
      "list_tickets",
      "/tickets",
      '{"type": "string"}',
      'The parameter schema must be a JSON Schema object with "type": "object".',
    ],
    [
      "list_tickets",
      "/tickets",
      '{"type": "object", "properties": {"a": {"$ref": "https://evil.test/s.json"}}}',
      "The parameter schema may only refer to itself ($ref starting with #).",
    ],
  ])("blocks Next for a tool named %j at %j with schema %j", (name, path, schema, message) => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    addTool(name, path);
    if (schema) {
      fireEvent.change(screen.getByPlaceholderText('{"type": "object", "properties": {…}}'), {
        target: { value: schema },
      });
    }
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByText(message)).toBeInTheDocument();
    // Still on the tools step.
    expect(screen.getByPlaceholderText("Tool name")).toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
  });

  it("blocks Next for two tools with the same name", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    addTool("list_tickets", "/tickets");
    addTool("list_tickets", "/tickets/open");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getAllByText("Another tool already has this name.")).toHaveLength(2);
    expect(screen.getAllByPlaceholderText("Tool name")).toHaveLength(2);
  });

  it("disables every capa left under an earlier name, even after two renames", async () => {
    let installs = 0;
    const disabled: string[] = [];
    let failedOnce = false;
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") {
        installs += 1;
        return Promise.resolve({ id: `cv-${installs}`, pluginId: `capa-${installs}` });
      }
      if (/^\/capas\/capa-\d\/enable$/.test(path)) return Promise.resolve({});
      const disable = /^\/capas\/(capa-\d)\/disable$/.exec(path);
      if (disable) {
        if (!failedOnce) {
          failedOnce = true;
          return Promise.reject(new Error("The old capa could not be disabled."));
        }
        disabled.push(disable[1]);
        return Promise.resolve({});
      }
      if (/^\/capas\/capa-\d\/setup$/.test(path)) {
        return Promise.resolve({ connectionId: `conn-${installs}` });
      }
      if (path.endsWith("/test-call")) return Promise.resolve(CALL_WORKED);
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    await testAPlainHttpApi();

    async function renameAndTest(name: string) {
      fireEvent.click(screen.getByRole("button", { name: /back/i }));
      fireEvent.click(screen.getByRole("button", { name: /back/i }));
      fireEvent.change(screen.getByLabelText("Name"), { target: { value: name } });
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    }
    await renameAndTest("Helpdesk");
    expect(await screen.findByText("The old capa could not be disabled.")).toBeInTheDocument();
    await renameAndTest("Support");
    await screen.findByText("The call worked.");
    expect(installs).toBe(3);
    expect(disabled.sort()).toEqual(["capa-1", "capa-2"]);
  });

  it("does not install the renamed capa again when disabling the old one failed", async () => {
    let installs = 0;
    let disables = 0;
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") {
        installs += 1;
        return Promise.resolve({ id: `cv-${installs}`, pluginId: `capa-${installs}` });
      }
      if (/^\/capas\/capa-\d\/enable$/.test(path)) return Promise.resolve({});
      if (/^\/capas\/capa-\d\/disable$/.test(path)) {
        disables += 1;
        return disables === 1
          ? Promise.reject(new Error("The old capa could not be disabled."))
          : Promise.resolve({});
      }
      if (/^\/capas\/capa-\d\/setup$/.test(path)) {
        return Promise.resolve({ connectionId: `conn-${installs}` });
      }
      if (path.endsWith("/test-call")) return Promise.resolve(CALL_WORKED);
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    await testAPlainHttpApi();

    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Helpdesk" } });
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    expect(await screen.findByText("The old capa could not be disabled.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/mcp/connections/conn-2/test-call", {
        tool: "list_tickets",
        arguments: {},
      }),
    );
    expect(installs).toBe(2);
    // The old capa is disabled on the retry instead.
    expect(disables).toBe(2);
  });

  it("keeps Back disabled while a test call runs", async () => {
    let answer: (value: unknown) => void = () => {};
    postMock.mockImplementation((path: string) => {
      if (path === "/capas") return Promise.resolve({ id: "cv-1", pluginId: "capa-1" });
      if (path === "/capas/capa-1/enable") return Promise.resolve({});
      if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
      if (path.endsWith("/test-call")) return new Promise((resolve) => (answer = resolve));
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
    fireEvent.click(screen.getByText("Plain HTTP API"));
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
      target: { value: "https://tickets.acme.test" },
    });
    addTool("list_tickets");
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /back/i })).toBeDisabled());
    answer({
      ok: true,
      status: 200,
      durationMs: 5,
      excerpt: "[]",
      truncated: false,
      errorClass: null,
      message: "The API answered 200.",
    });
    await screen.findByText("The call worked.");
    expect(screen.getByRole("button", { name: /back/i })).toBeEnabled();
  });

  describe("department", () => {
    const departments = {
      items: [
        { id: "d-support", name: "Support" },
        { id: "d-sales", name: "Sales" },
      ],
      totalCount: 2,
    };

    function mockInstall() {
      postMock.mockImplementation((path: string) => {
        if (path === "/capas") {
          return Promise.resolve({
            id: "cv-1",
            pluginId: "capa-1",
            name: "Tickets",
            type: "tool_pack",
            semver: "1.0.0",
            trustLevel: "unverified",
          });
        }
        if (path === "/capas/capa-1/enable") return Promise.resolve({});
        if (path === "/capas/capa-1/setup") return Promise.resolve({ connectionId: "conn-1" });
        return Promise.reject(new Error(`unexpected POST ${path}`));
      });
    }

    async function nameAndType() {
      fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tickets" } });
      fireEvent.click(screen.getByText("Plain HTTP API"));
    }

    it("places the connection in the department it was opened from", async () => {
      getMock.mockResolvedValue(departments);
      mockInstall();
      render(<CustomMcpWizard open onOpenChange={() => {}} defaultDepartmentId="d-sales" />, {
        wrapper,
      });
      await nameAndType();
      const select = (await screen.findByLabelText("Department")) as HTMLSelectElement;
      await waitFor(() => expect(select.value).toBe("d-sales"));
      fireEvent.click(screen.getByRole("button", { name: /next/i }));

      fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
        target: { value: "https://tickets.acme.test" },
      });
      addTool();
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

      await waitFor(() =>
        expect(postMock).toHaveBeenCalledWith("/capas/capa-1/setup", {
          values: { department: "d-sales" },
        }),
      );
      const install = postMock.mock.calls.find(([path]) => path === "/capas");
      const setup = install?.[1].manifest.setup;
      expect(setup.fields).toContainEqual(
        expect.objectContaining({ key: "department", kind: "department", required: true }),
      );
      expect(setup.mcp.department_field).toBe("department");
    });

    // Tests the API in Support, goes back, picks Sales and optionally edits
    // the tool, then tests again.
    async function testThenMoveToSales(editTool: boolean) {
      // One connection per department, as the server keeps them.
      const byDepartment: Record<string, string> = { "d-support": "conn-1", "d-sales": "conn-2" };
      postMock.mockImplementation((path: string, body?: { values?: Record<string, string> }) => {
        if (path === "/capas") return Promise.resolve({ id: "cv-1", pluginId: "capa-1" });
        if (path === "/capas/capa-1/enable") return Promise.resolve({});
        if (path === "/capas/capa-1/setup") {
          return Promise.resolve({
            connectionId: byDepartment[body?.values?.department ?? ""] ?? null,
          });
        }
        if (path.endsWith("/test-call")) {
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
      getMock.mockResolvedValue(departments);
      render(<CustomMcpWizard open onOpenChange={() => {}} defaultDepartmentId="d-support" />, {
        wrapper,
      });
      await nameAndType();
      const select = (await screen.findByLabelText("Department")) as HTMLSelectElement;
      await waitFor(() => expect(select.value).toBe("d-support"));
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.change(screen.getByPlaceholderText("https://api.example.com"), {
        target: { value: "https://tickets.acme.test" },
      });
      addTool();
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
      await screen.findByText("The call worked.");

      fireEvent.click(screen.getByRole("button", { name: /back/i }));
      if (editTool) {
        fireEvent.change(screen.getByPlaceholderText("Tool name"), {
          target: { value: "list_open_tickets" },
        });
      }
      fireEvent.click(screen.getByRole("button", { name: /back/i }));
      fireEvent.change(screen.getByLabelText("Department"), { target: { value: "d-sales" } });
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: "Run test call" }));
      await waitFor(() =>
        expect(postMock).toHaveBeenCalledWith("/mcp/connections/conn-2/test-call", {
          tool: editTool ? "list_open_tickets" : "list_tickets",
          arguments: {},
        }),
      );
      expect(postMock).toHaveBeenLastCalledWith("/mcp/connections/conn-2/test-call", {
        tool: editTool ? "list_open_tickets" : "list_tickets",
        arguments: {},
      });
      expect(postMock).toHaveBeenCalledWith("/capas/capa-1/setup", {
        values: { department: "d-sales" },
      });
    }

    it("moves the connection to the new department when the tools changed too", async () => {
      await testThenMoveToSales(true);
      expect(patchMock).toHaveBeenCalledTimes(1);
      expect(patchMock.mock.calls[0][0]).toBe("/mcp/connections/conn-1/http-tools");
      // The first department's connection, with its login, does not stay behind.
      expect(deleteMock).toHaveBeenCalledWith("/mcp/connections/conn-1");
      expect(deleteMock).toHaveBeenCalledTimes(1);
      expect(postMock.mock.calls.filter(([path]) => path === "/capas")).toHaveLength(1);
    });

    it("moves the connection to the new department when only the department changed", async () => {
      await testThenMoveToSales(false);
      expect(patchMock).not.toHaveBeenCalled();
      expect(deleteMock).toHaveBeenCalledWith("/mcp/connections/conn-1");
      expect(deleteMock).toHaveBeenCalledTimes(1);
    });

    it("removes the left department's connection on the next save when removing it failed", async () => {
      deleteMock.mockRejectedValueOnce(new Error("busy"));
      await testThenMoveToSales(false);
      expect(toastWarning).toHaveBeenCalledTimes(1);
      expect(deleteMock).toHaveBeenCalledTimes(1);

      fireEvent.click(screen.getByRole("button", { name: /next/i }));
      fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
      await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(2));
      expect(deleteMock).toHaveBeenLastCalledWith("/mcp/connections/conn-1");
      expect(toastWarning).toHaveBeenCalledTimes(1);
    });

    it("needs a department before going on when the organisation has departments", async () => {
      getMock.mockResolvedValue(departments);
      render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
      await nameAndType();
      const select = (await screen.findByLabelText("Department")) as HTMLSelectElement;
      expect(select.value).toBe("");
      expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();

      fireEvent.change(select, { target: { value: "d-support" } });
      expect(screen.getByRole("button", { name: /next/i })).toBeEnabled();
    });

    it("asks for no department in an organisation without departments", async () => {
      mockInstall();
      render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
      await nameAndType();
      expect(screen.queryByLabelText("Department")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /next/i })).toBeEnabled();
    });
  });
});
