// Speech models (spec 2026-11-27 §9) never run an agent: the new-agent
// dialog neither lists nor preselects one. Same setup as new-agent-dialog.test.tsx.
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { NewAgentDialog } from "@/components/new-agent-dialog";

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

const createAgentMock = vi.fn().mockResolvedValue({ id: "new-agent-id", currentVersionNo: 1 });
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
  // The speech model comes first: it must be neither listed nor preselected.
  useModels: () => ({
    data: [
      { id: "stt", name: "Whisper", provider: "openai", capability: "speech_to_text" },
      { id: "m1", name: "GPT", provider: "openai", capability: "chat" },
    ],
  }),
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

const publishHireMock = vi.fn().mockResolvedValue(undefined);
vi.mock("@/lib/agent-versions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/agent-versions")>();
  return { ...actual, usePublishHireFollowUps: () => ({ mutateAsync: publishHireMock }) };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

function renderDialog(defaultDepartmentId?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NewAgentDialog open onOpenChange={() => {}} defaultDepartmentId={defaultDepartmentId} />
    </QueryClientProvider>,
  );
}

describe("NewAgentDialog with a speech model", () => {
  it("preselects and lists only chat models", async () => {
    renderDialog();
    await screen.findByText("Hire a new agent");
    fireEvent.change(screen.getByPlaceholderText(/Nova, Atlas, Miro/i), {
      target: { value: "Nora" },
    });
    fireEvent.change(screen.getByPlaceholderText(/Customer service/i), {
      target: { value: "Sales" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> LLM
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(1);
    expect(screen.queryByText("Whisper")).not.toBeInTheDocument();
    expect(radios[0]).toBeChecked();
    expect(screen.getByText("GPT")).toBeInTheDocument();
    await waitFor(() => expect(radios[0]).toBeChecked());
  });
});
