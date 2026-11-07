import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { AgentDetail } from "@/lib/hooks-agent-detail";

const updateMutate = vi.fn();
vi.mock("@/lib/hooks-agent-detail", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-agent-detail")>();
  return { ...actual, useUpdateNarrowing: () => ({ mutate: updateMutate, isPending: false }) };
});

const createLoginMutateAsync = vi.fn();
// A mutable ref so the regression test below can hand back a "github"
// connection with a credentialType (needed to make AgentGuardrailsPanel
// render a loginPicker at all), while the first test keeps the default
// empty list -- vi.hoisted is required here because vi.mock's factory is
// hoisted above any plain const/let in this module.
const { mockConnectionsRef } = vi.hoisted(() => ({
  mockConnectionsRef: { current: [] as unknown[] },
}));
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useMcpConnections: () => ({ data: mockConnectionsRef.current }),
    useMcpLogins: () => ({ data: [] }),
    useCreateMcpLogin: () => ({ mutateAsync: createLoginMutateAsync }),
    useConnectionToolNames: () => ({ data: { names: [] } }),
  };
});

// `CredentialPicker` itself is a separate, already-tested component (its
// internal <select>/"create new" behavior isn't this test's concern) --
// stubbed here to a single button that fires `onChange` with a fixed
// credential id, so the regression test below can drive
// `AgentGuardrailsPanel`'s own connection-id plumbing without depending on
// `CredentialPicker`'s internal hooks (useCredentials/useCredentialTypes).
vi.mock("@/components/credential-picker", () => ({
  CredentialPicker: ({ onChange }: { onChange: (credentialId: string) => void }) => (
    <button type="button" onClick={() => onChange("cred-999")}>
      mock-credential-picker
    </button>
  ),
}));

vi.mock("@/components/member-guardrails-sheet", () => ({
  MemberGuardrailsPanel: ({ agentId }: { agentId: string }) => (
    <div data-testid="member-panel">{agentId}</div>
  ),
  MemberGuardrailsInsight: () => <div data-testid="member-insight" />,
}));

import { AgentGuardrailsPanel, AgentGuardrailsScopes } from "@/routes/agents.$id";

const AGENT: Partial<AgentDetail> = {
  id: "agent-1",
  name: "Nora",
  departmentFrameTools: {
    github: {
      enabled: true,
      read: true,
      modify: false,
      approvalEur: null,
      approvalActions: [],
      only: null,
    },
  },
  effectiveTools: {
    github: {
      enabled: true,
      read: true,
      modify: false,
      approvalEur: null,
      approvalActions: [],
      only: null,
    },
  },
  narrowingTools: {},
};

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AgentGuardrailsPanel agent={AGENT as AgentDetail} mayManage />
    </QueryClientProvider>,
  );
}

describe("AgentGuardrailsPanel", () => {
  it("shows the inherited status for a tool with no narrowing override", () => {
    renderPanel();
    expect(screen.getByText(/wie department|same as department/i)).toBeInTheDocument();
  });

  it("shows inherited (not narrowed) for a tool present in narrowingTools but not in narrowingOverriddenKeys", () => {
    // Regression: every save rewrites every currently-relevant key whether
    // or not it's the one being edited, so `narrowingTools` can carry an
    // entry for a tool the agent never deliberately touched. The status
    // badge must key off `narrowingOverriddenKeys` (the one place this is
    // tracked explicitly), not mere presence in `narrowingTools` -- a live-
    // verification finding where every agent with ANY saved narrowing
    // showed "Narrowed" on every tool, not just the ones actually narrowed.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <AgentGuardrailsPanel
          agent={
            {
              ...AGENT,
              narrowingTools: {
                github: {
                  enabled: true,
                  read: true,
                  modify: false,
                  approvalEur: null,
                  approvalActions: [],
                  only: null,
                },
              },
              narrowingOverriddenKeys: [],
            } as AgentDetail
          }
          mayManage
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText(/wie department|same as department/i)).toBeInTheDocument();
    expect(screen.queryByText(/eingeschränkt|narrowed/i)).not.toBeInTheDocument();
  });

  it("threads a newly picked login's connection id into the same save call", async () => {
    createLoginMutateAsync.mockResolvedValue({ id: "login-abc" });
    mockConnectionsRef.current = [
      {
        id: "conn-1",
        name: "github",
        transport: "stdio",
        serverUrl: "",
        command: "",
        args: [],
        departmentId: null,
        connected: true,
        scopes: [],
        health: {},
        guardrailPresets: [],
        guardrailLibrary: null,
        hasValueSpec: false,
        pluginName: null,
        credentialType: "github_token",
      },
    ];
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: "mock-credential-picker" }));

    await waitFor(() => expect(updateMutate).toHaveBeenCalled());
    const [payload] = updateMutate.mock.calls[updateMutate.mock.calls.length - 1];
    expect(payload.narrowing.tools.github.connection_id).toBe("login-abc");
  });
});

describe("AgentGuardrailsScopes", () => {
  function renderScopes(mayManage: boolean) {
    const qc = new QueryClient();
    return render(
      <QueryClientProvider client={qc}>
        <AgentGuardrailsScopes agent={AGENT as AgentDetail} mayManage={mayManage} />
      </QueryClientProvider>,
    );
  }

  it("gives a manager both 'For the agent' and 'For me', with the member insight", () => {
    renderScopes(true);
    expect(screen.getByRole("tab", { name: "For the agent" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("member-insight")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "For me" }), { button: 0 });
    expect(screen.getByTestId("member-panel")).toHaveTextContent("agent-1");
  });

  it("shows a member without manage rights only 'For me'", () => {
    renderScopes(false);
    expect(screen.queryByRole("tab", { name: "For the agent" })).toBeNull();
    expect(screen.queryByTestId("member-insight")).toBeNull();
    expect(screen.getByTestId("member-panel")).toHaveTextContent("agent-1");
  });
});
