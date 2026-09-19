import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CustomMcpWizard } from "@/components/custom-mcp-wizard";

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: {
    post: postMock,
    get: vi.fn(),
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
});
