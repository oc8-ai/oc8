import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CustomMcpWizard } from "@/components/custom-mcp-wizard";

const { postMock, getMock } = vi.hoisted(() => ({ postMock: vi.fn(), getMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: {
    post: postMock,
    get: getMock,
    patch: vi.fn(),
    delete: vi.fn(),
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

describe("CustomMcpWizard", () => {
  beforeEach(() => {
    postMock.mockReset();
    getMock.mockReset();
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
    fireEvent.click(screen.getByRole("button", { name: /add tool/i }));
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
    fireEvent.click(screen.getByRole("button", { name: /add tool/i }));
    fireEvent.change(screen.getByPlaceholderText("Tool name"), {
      target: { value: "list_tickets" },
    });
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
      fireEvent.click(screen.getByRole("button", { name: /add tool/i }));
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
