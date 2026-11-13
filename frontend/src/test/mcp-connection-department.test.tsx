// A connection in no department reaches no agent (the MCP gateway hands a run
// only its own department's connections) -- its card says so and lets an
// operator move it into one.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpConnectionBody } from "@/components/mcp-connections";
import type { McpConnection } from "@/lib/hooks";

const { getMock, patchMock } = vi.hoisted(() => ({ getMock: vi.fn(), patchMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: { get: getMock, post: vi.fn(), patch: patchMock, delete: vi.fn() },
}));

function wrapper({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

function connection(patch: Partial<McpConnection> = {}): McpConnection {
  return {
    id: "conn-1",
    name: "tickets",
    transport: "http",
    serverUrl: "https://tickets.acme.test/mcp",
    command: "",
    args: [],
    departmentId: null,
    needsDepartment: true,
    connected: true,
    scopes: [],
    health: { status: "ok", tools: [] },
    guardrailPresets: [],
    guardrailLibrary: null,
    guardrailAttributes: [],
    hasValueSpec: false,
    ...patch,
  } as McpConnection;
}

describe("McpConnectionBody department", () => {
  beforeEach(() => {
    getMock.mockReset();
    patchMock.mockReset();
    getMock.mockResolvedValue({
      items: [
        { id: "d-support", name: "Support" },
        { id: "d-sales", name: "Sales" },
      ],
      totalCount: 2,
    });
    patchMock.mockResolvedValue(connection({ departmentId: "d-sales", needsDepartment: false }));
  });

  it("says a connection in no department reaches no agent and moves it into one", async () => {
    render(<McpConnectionBody connection={connection()} />, { wrapper });
    expect(screen.getByText(/in no department/i)).toBeInTheDocument();

    const select = (await screen.findByLabelText("Department")) as HTMLSelectElement;
    await screen.findByRole("option", { name: "Sales" });
    fireEvent.change(select, { target: { value: "d-sales" } });

    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith("/mcp/connections/conn-1", {
        departmentId: "d-sales",
      }),
    );
  });

  it("shows nothing of the sort for a connection that already reaches its agents", () => {
    render(
      <McpConnectionBody
        connection={connection({ departmentId: "d-support", needsDepartment: false })}
      />,
      { wrapper },
    );
    expect(screen.queryByText(/in no department/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Department")).not.toBeInTheDocument();
  });
});
