import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
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

const logins = [
  {
    id: "login-1",
    name: "Odoo",
    credentialId: "cred-1",
    departmentId: null,
    connected: true,
    scopes: [],
    health: {},
  },
];

const createAgentMock = vi.fn().mockResolvedValue({ id: "new-agent-id" });
const createTriggerMock = vi.fn().mockResolvedValue({});
const credentialTypesMock = vi.fn().mockReturnValue({ data: [] });
const credentialsMock = vi.fn().mockReturnValue({ data: [] });
const connectionsMock = vi.fn().mockReturnValue({ data: connections });
const useMcpLoginsMock = vi.fn().mockReturnValue({ data: logins });
const createLoginMock = vi.fn();
const createCredentialMock = vi.fn();
const departmentToolsMock = vi.fn().mockReturnValue({
  data: { tools: { Odoo: { enabled: true, read: true, modify: true } } },
});

vi.mock("@/lib/hooks", () => ({
  useModels: () => ({ data: [{ id: "m1", name: "GPT", provider: "OpenAI" }] }),
  useModelProviders: () => ({ data: [] }),
  useMcpConnections: () => connectionsMock(),
  useMcpLogins: () => useMcpLoginsMock(),
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

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

function renderDialog(defaultDepartmentId?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NewAgentDialog open onOpenChange={() => {}} defaultDepartmentId={defaultDepartmentId} />
    </QueryClientProvider>,
  );
}

function fillIdentityAndGoToTools() {
  fireEvent.change(screen.getByPlaceholderText(/Nova, Atlas, Miro/i), {
    target: { value: "Nora" },
  });
  fireEvent.change(screen.getByPlaceholderText(/Customer service/i), {
    target: { value: "Sales" },
  });
  fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> LLM
  fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Tools
}

beforeEach(() => {
  createAgentMock.mockClear();
  createTriggerMock.mockClear();
  navigateMock.mockClear();
  credentialTypesMock.mockReset();
  credentialTypesMock.mockReturnValue({ data: [] });
  credentialsMock.mockReset();
  credentialsMock.mockReturnValue({ data: [] });
  connectionsMock.mockReset();
  connectionsMock.mockReturnValue({ data: connections });
  useMcpLoginsMock.mockReset();
  useMcpLoginsMock.mockReturnValue({ data: logins });
  createLoginMock.mockReset();
  createCredentialMock.mockReset();
  departmentToolsMock.mockReset();
  departmentToolsMock.mockReturnValue({
    data: { tools: { Odoo: { enabled: true, read: true, modify: true } } },
  });
});

describe("NewAgentDialog: hiring wording + department pre-selection", () => {
  it("uses hire wording throughout, not create", async () => {
    renderDialog();
    expect(await screen.findByText("Hire a new agent")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Nova, Atlas, Miro/i), {
      target: { value: "Nora" },
    });
    fireEvent.change(screen.getByPlaceholderText(/Customer service/i), {
      target: { value: "Sales" },
    });
    for (let i = 0; i < 3; i++) {
      fireEvent.click(screen.getByRole("button", { name: /Next/i }));
    }
    expect(screen.getByRole("button", { name: /Hire agent/i })).toBeInTheDocument();
  });

  it("pre-selects the department passed via defaultDepartmentId", async () => {
    renderDialog("dept-b");
    const select = (await screen.findByLabelText("Department")) as HTMLSelectElement;
    expect(select.value).toBe("dept-b");
  });

  it("falls back to the first department when defaultDepartmentId matches none", async () => {
    renderDialog("does-not-exist");
    const select = (await screen.findByLabelText("Department")) as HTMLSelectElement;
    expect(select.value).toBe("dept-a");
  });
});

describe("NewAgentDialog: inline credential picker", () => {
  it("shows the inline credential picker for a tool whose manifest declares a credential type", async () => {
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText(/select a credential/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create new/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/approval/i)).not.toBeInTheDocument();
  });

  it("shows no credential picker for a tool whose manifest doesn't declare a credential type", async () => {
    renderDialog(); // default fixture: connections[0].credentialType is null
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.queryByText(/select a credential/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create new/i })).not.toBeInTheDocument();
  });

  it("shows the picker even for a tool not yet enabled in the target department's frame, with a hint that the pick won't grant access yet", async () => {
    // Live user feedback: the picker only appeared for Odoo because it was
    // the one tool already in the test department's frame -- every checked
    // tool with a known credential type should offer it automatically.
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    departmentToolsMock.mockReturnValue({ data: { tools: {} } }); // Odoo not in frame
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText(/select a credential/i)).toBeInTheDocument();
    expect(screen.getByText(/not yet enabled for this department/i)).toBeInTheDocument();
  });

  it("blocks hiring when a login-capable tool is selected without a login picked", async () => {
    const { toast } = await import("sonner");
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(createAgentMock).not.toHaveBeenCalled();
  });

  it("hires with an existing credential (already backing a login) wired into narrowing", async () => {
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    credentialsMock.mockReturnValue({
      data: [{ id: "cred-1", name: "oc8-local", credentialType: "odoo_login" }],
    });
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    // "cred-1" is the credential logins[0] ("login-1") is already backed by
    // -- picking it reuses that login, no POST /mcp/logins round trip.
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });
    await waitFor(() =>
      expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("cred-1"),
    );
    expect(createLoginMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createAgentMock).toHaveBeenCalled());
    const body = createAgentMock.mock.calls[0][0];
    expect(body.narrowing.tools.Odoo).toMatchObject({
      enabled: true,
      read: true,
      modify: true,
      approval_eur: null,
      connection_id: "login-1",
    });
  });

  it("does not reuse a login scoped to a different department, even if it shares the tool key and credential", async () => {
    // Agent tool login selection design's department-scoping extension:
    // "login-1" belongs to dept-b, this agent is hired into dept-a
    // (departments[0], the default preselection) -- picking the same
    // credential must mint a NEW, dept-a-scoped login instead of silently
    // reusing dept-b's.
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    credentialsMock.mockReturnValue({
      data: [{ id: "cred-1", name: "oc8-local", credentialType: "odoo_login" }],
    });
    useMcpLoginsMock.mockReturnValue({
      data: [{ ...logins[0], departmentId: "dept-b" }],
    });
    createLoginMock.mockResolvedValue({ id: "new-login-2", name: "Odoo" });

    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });
    await waitFor(() =>
      expect(createLoginMock).toHaveBeenCalledWith({
        name: "Odoo",
        credentialType: "odoo_login",
        credentialId: "cred-1",
        scopes: [],
        departmentId: "dept-a",
      }),
    );
  });

  it("creates a new credential inline and pins its login immediately -- no separate modal, no free-text tool scopes", async () => {
    // Live user feedback: a stacked modal asking for raw tool-call names
    // "das bekommt doch kein mitarbeiter hin" -- creating a credential now
    // happens inline, the same "select or create new" control the Capa
    // setup form uses, with no scopes field at all.
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    credentialsMock.mockReturnValue({ data: [] });
    credentialTypesMock.mockReturnValue({
      data: [
        {
          name: "odoo_login",
          displayName: "Odoo login",
          fields: [
            {
              key: "password",
              label: "Password",
              kind: "password",
              required: true,
              default: "",
              placeholder: "",
              help: "",
            },
          ],
        },
      ],
    });
    createCredentialMock.mockResolvedValue({ id: "new-cred-1" });
    createLoginMock.mockResolvedValue({ id: "new-login-1", name: "Odoo" });

    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    fireEvent.click(screen.getByRole("button", { name: /create new/i }));
    expect(screen.queryByLabelText(/tool/i)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Password", { exact: false }), {
      target: { value: "secret1" },
    });
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: "oc8-local" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(createCredentialMock).toHaveBeenCalledWith({
        name: "oc8-local",
        credentialType: "odoo_login",
        fieldValues: { password: "secret1" },
      }),
    );
    await waitFor(() =>
      expect(createLoginMock).toHaveBeenCalledWith({
        name: "Odoo",
        credentialType: "odoo_login",
        credentialId: "new-cred-1",
        scopes: [],
        departmentId: "dept-a",
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createAgentMock).toHaveBeenCalled());
    const body = createAgentMock.mock.calls[0][0];
    expect(body.narrowing.tools.Odoo.connection_id).toBe("new-login-1");
  });
});

describe("NewAgentDialog: real schedule + webhook triggers", () => {
  it("shows the cron builder once Schedule is picked, and creates a real trigger on hire", async () => {
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /^Schedule$/i }));
    expect(screen.getByText(/Frequency/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createTriggerMock).toHaveBeenCalled());
    expect(createTriggerMock.mock.calls[0][0]).toMatchObject({
      agentId: "new-agent-id",
      // A new schedule is read in the organisation's timezone by default.
      timezone: "Europe/Berlin",
    });
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("sends the timezone picked for the schedule", async () => {
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /^Schedule$/i }));
    const zone = screen.getByLabelText("Timezone") as HTMLInputElement;
    expect(zone.value).toBe("Europe/Berlin");
    fireEvent.change(zone, { target: { value: "America/New_York" } });
    expect(screen.getByText(/\(America\/New_York\)$/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createTriggerMock).toHaveBeenCalled());
    expect(createTriggerMock.mock.calls[0][0]).toMatchObject({ timezone: "America/New_York" });
  });

  it("enables the Webhook trigger option, creates a kind='webhook' trigger on hire, and navigates to the agent's page", async () => {
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    const webhookButton = screen.getByRole("button", { name: /^Webhook/i });
    expect(webhookButton).not.toBeDisabled();
    fireEvent.click(webhookButton);
    expect(screen.getByText(/unique webhook URL is generated/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createTriggerMock).toHaveBeenCalled());
    expect(createTriggerMock.mock.calls[0][0]).toMatchObject({
      agentId: "new-agent-id",
      kind: "webhook",
    });
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith({
        to: "/agents/$id",
        params: { id: "new-agent-id" },
      }),
    );
  });
});
