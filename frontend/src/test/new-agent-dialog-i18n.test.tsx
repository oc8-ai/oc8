import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";

const { navigateMock } = vi.hoisted(() => ({ navigateMock: vi.fn() }));

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return { ...actual, useNavigate: () => navigateMock };
});

const departments = [
  { id: "dept-a", name: "Vertrieb" },
  { id: "dept-b", name: "Support" },
];

const connections = [
  {
    id: "conn-1",
    name: "Odoo",
    transport: "stdio",
    serverUrl: "",
    command: "",
    args: [],
    departmentId: null,
    connected: true,
    scopes: [],
    health: {},
    credentialType: null as string | null,
    guardrailPresets: [] as unknown[],
    guardrailLibrary: null,
    hasValueSpec: false,
    pluginName: null,
  },
];

const createAgentMock = vi.fn().mockResolvedValue({ id: "new-agent-id" });
const createTriggerMock = vi.fn().mockResolvedValue({});
const credentialTypesMock = vi.fn().mockReturnValue({ data: [] });
const credentialsMock = vi.fn().mockReturnValue({ data: [] });
const connectionsMock = vi.fn().mockReturnValue({ data: connections });
const createLoginMock = vi.fn();
const createCredentialMock = vi.fn();
const departmentToolsMock = vi.fn().mockReturnValue({
  data: { tools: { Odoo: { enabled: true, read: true, modify: true } } },
});

vi.mock("@/lib/hooks", () => ({
  useModels: () => ({ data: [{ id: "m1", name: "GPT", provider: "OpenAI" }] }),
  useModelProviders: () => ({ data: [] }),
  useMcpConnections: () => connectionsMock(),
  useCredentialTypes: () => credentialTypesMock(),
  useCredentials: () => credentialsMock(),
  useCreateCredential: () => ({ mutateAsync: createCredentialMock, isPending: false }),
  useCreateMcpLogin: () => ({ mutateAsync: createLoginMock }),
  useDepartmentTools: () => departmentToolsMock(),
  useDepartments: () => ({ data: { items: departments, totalCount: departments.length } }),
  useCreateAgent: () => ({ mutateAsync: createAgentMock, isPending: false }),
  useCreateAgentTrigger: () => ({ mutateAsync: createTriggerMock }),
  useAuth: () => ({ data: { organisationTimezone: "Europe/Berlin" } }),
}));

const setNarrowingMock = vi.fn().mockResolvedValue({});
vi.mock("@/lib/hooks-agent-detail", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-agent-detail")>();
  return { ...actual, useSetAgentNarrowing: () => ({ mutateAsync: setNarrowingMock }) };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return { ...actual, useT: () => (_en: string, de?: string) => de ?? _en };
});

import { NewAgentDialog } from "@/components/new-agent-dialog";

// Review of F5/F15: the hire wizard's own texts stayed English in German.
describe("NewAgentDialog in German", () => {
  it("speaks German through the steps it shows", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <NewAgentDialog open onOpenChange={() => {}} />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Neuen Agenten einstellen")).toBeInTheDocument();
    expect(screen.getByText(/Schritt 1 von 4/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Weiter/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Abbrechen/ })).toBeInTheDocument();
    for (const english of ["Hire a new agent", "Next", "Cancel"]) {
      expect(screen.queryByText(english)).not.toBeInTheDocument();
    }
  });
});
