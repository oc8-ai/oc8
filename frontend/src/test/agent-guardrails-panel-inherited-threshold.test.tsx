import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { AgentDetail } from "@/lib/hooks-agent-detail";

// E2E F3: adding a tool (or saving any guardrail) on an agent rewrites the
// whole tool set, so a department threshold on another tool travels along.
// On a connection with no value spec it no longer does: nothing could enforce
// an agent's copy, the frame's own applies anyway, and a copy saved once went
// stale the moment the department changed its value.

const updateMutate = vi.fn();
vi.mock("@/lib/hooks-agent-detail", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-agent-detail")>();
  return { ...actual, useUpdateNarrowing: () => ({ mutate: updateMutate, isPending: false }) };
});
vi.mock("@/lib/connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/connections")>();
  return {
    ...actual,
    useAgentToolAccess: () => ({ data: [] }),
    useSetAgentToolAccess: () => ({ mutateAsync: vi.fn() }),
    useResetAgentToolAccess: () => ({ mutateAsync: vi.fn() }),
  };
});
const { connectionsData } = vi.hoisted(() => ({
  connectionsData: { current: [] as Array<{ name: string; hasValueSpec: boolean }> },
}));
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useMcpConnections: () => ({ data: connectionsData.current }),
    useMcpLogins: () => ({ data: [] }),
  };
});

type TableProps = {
  onAdd: (name: string, policy: unknown, connectionId?: string | null) => void;
};
const { tableProps } = vi.hoisted(() => ({ tableProps: { current: null as TableProps | null } }));
vi.mock("@/components/tool-guardrail-table", () => ({
  ToolGuardrailTable: (props: TableProps) => {
    tableProps.current = props;
    return null;
  },
}));

import { AgentGuardrailsPanel } from "@/routes/agents.$id";

const M365 = {
  enabled: true,
  read: true,
  modify: true,
  approvalEur: 200,
  approvalActions: [],
  only: null,
};

const AGENT: Partial<AgentDetail> = {
  id: "agent-1",
  name: "Nora",
  departmentFrameTools: { microsoft365: M365 },
  effectiveTools: { microsoft365: M365 },
  narrowingTools: {},
};

function addEmail(): Record<string, Record<string, unknown>> {
  updateMutate.mockClear();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <AgentGuardrailsPanel agent={AGENT as AgentDetail} mayManage />
    </QueryClientProvider>,
  );
  tableProps.current!.onAdd("email", null);
  expect(updateMutate).toHaveBeenCalledTimes(1);
  return updateMutate.mock.calls[0][0].narrowing.tools;
}

describe("AgentGuardrailsPanel inherited threshold", () => {
  it("echoes the department's threshold unchanged on a connection with a value spec", () => {
    connectionsData.current = [{ name: "microsoft365", hasValueSpec: true }];
    const tools = addEmail();
    expect(tools.email).toMatchObject({ enabled: true, read: true, approval_eur: null });
    // Dropping it would trip "narrowing removes a frame approval threshold";
    // changing it would be a deliberate narrowing the user never made.
    expect(tools.microsoft365).toMatchObject({ enabled: true, approval_eur: 200 });
  });

  it("leaves it out on a connection without a value spec", () => {
    connectionsData.current = [{ name: "microsoft365", hasValueSpec: false }];
    const tools = addEmail();
    expect(tools.microsoft365).toMatchObject({ enabled: true });
    expect(tools.microsoft365).not.toHaveProperty("approval_eur");
  });
});
