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
  createLoginMock.mockReset();
  createCredentialMock.mockReset();
  setNarrowingMock.mockClear();
  publishHireMock.mockClear();
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

  it("grants a tool the department doesn't offer to the new agent alone, after fixing its login (E2E F5)", async () => {
    // The wizard used to keep such a pick presentation-only: the agent's
    // narrowing stayed {} although a login was fixed for it, and the card
    // said both "enabled for all departments" and "not yet enabled".
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    credentialsMock.mockReturnValue({
      data: [{ id: "cred-1", name: "oc8-local", credentialType: "odoo_login" }],
    });
    createLoginMock.mockResolvedValue({ id: "cred-1", name: "Odoo" });
    departmentToolsMock.mockReturnValue({ data: { tools: {} } }); // Odoo not in frame
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText(/only for this agent/i)).toBeInTheDocument();
    expect(screen.queryByText(/not yet enabled for this department/i)).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });

    fireEvent.click(screen.getByRole("button", { name: /Next/i }));
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(setNarrowingMock).toHaveBeenCalled());
    expect(setNarrowingMock).toHaveBeenCalledWith({
      agentId: "new-agent-id",
      narrowing: {
        tools: {
          Odoo: expect.objectContaining({ enabled: true, read: true, modify: false }),
        },
      },
    });
    // The login is fixed first, so the grant finds it.
    expect(createLoginMock.mock.invocationCallOrder[0]).toBeLessThan(
      setNarrowingMock.mock.invocationCallOrder[0],
    );
    // Both are part of the hire, so published with it: a new agent starts
    // with no unpublished changes (E2E N9).
    await waitFor(() =>
      expect(publishHireMock).toHaveBeenCalledWith({
        agentId: "new-agent-id",
        expectedCurrentVersionNo: 1,
      }),
    );
    expect(setNarrowingMock.mock.invocationCallOrder[0]).toBeLessThan(
      publishHireMock.mock.invocationCallOrder[0],
    );
  });

  it("says a tool the department turned off can't be used yet, and grants nothing for it", async () => {
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    departmentToolsMock.mockReturnValue({
      data: { tools: { Odoo: { enabled: false, read: true, modify: true } } },
    });
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText(/turned off for this department/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Next/i }));
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createAgentMock).toHaveBeenCalled());
    expect(createAgentMock.mock.calls[0][0].narrowing).toBeUndefined();
    expect(setNarrowingMock).not.toHaveBeenCalled();
  });

  it("hires without a login picked, leaving the login to the department", async () => {
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createAgentMock).toHaveBeenCalled());
    expect(createAgentMock.mock.calls[0][0].narrowing.tools.Odoo.connection_id).toBeNull();
    expect(createLoginMock).not.toHaveBeenCalled();
  });

  it("hires with a picked credential as the agent's fixed login, not a new connection", async () => {
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    credentialsMock.mockReturnValue({
      data: [{ id: "cred-1", name: "oc8-local", credentialType: "odoo_login" }],
    });
    createLoginMock.mockResolvedValue({ id: "cred-1", name: "Odoo" });
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });
    await waitFor(() =>
      expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("cred-1"),
    );
    // Nothing is created before the agent exists.
    expect(createLoginMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createAgentMock).toHaveBeenCalled());
    const body = createAgentMock.mock.calls[0][0];
    expect(body.narrowing.tools.Odoo).toMatchObject({
      enabled: true,
      read: true,
      modify: true,
      connection_id: null,
    });
    await waitFor(() =>
      expect(createLoginMock).toHaveBeenCalledWith({
        name: "Odoo",
        credentialType: "odoo_login",
        credentialId: "cred-1",
        scopes: [],
        agentId: "new-agent-id",
      }),
    );
    // The pinned login is part of the hire too: published after it (E2E N9).
    await waitFor(() => expect(publishHireMock).toHaveBeenCalled());
    expect(createLoginMock.mock.invocationCallOrder[0]).toBeLessThan(
      publishHireMock.mock.invocationCallOrder[0],
    );
  });

  it("publishes nothing on a hire that awaits its approval (review of round 6)", async () => {
    // The approver approves what was requested; the server refuses it too.
    createAgentMock.mockResolvedValueOnce({
      id: "new-agent-id",
      currentVersionNo: 1,
      awaitingHireApproval: true,
    });
    connectionsMock.mockReturnValue({
      data: [{ ...connections[0], credentialType: "odoo_login" }],
    });
    credentialsMock.mockReturnValue({
      data: [{ id: "cred-1", name: "oc8-local", credentialType: "odoo_login" }],
    });
    createLoginMock.mockResolvedValue({ id: "cred-1", name: "Odoo" });
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "cred-1" } });
    fireEvent.click(screen.getByRole("button", { name: /Next/i }));
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createLoginMock).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 50)); // the rest of finish()
    expect(publishHireMock).not.toHaveBeenCalled();
  });

  it("creates a new credential inline and fixes it for the agent on hire -- no separate modal, no free-text tool scopes", async () => {
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
    createLoginMock.mockResolvedValue({ id: "new-cred-1", name: "Odoo" });

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
    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createAgentMock).toHaveBeenCalled());
    const body = createAgentMock.mock.calls[0][0];
    expect(body.narrowing.tools.Odoo.connection_id).toBeNull();
    await waitFor(() =>
      expect(createLoginMock).toHaveBeenCalledWith({
        name: "Odoo",
        credentialType: "odoo_login",
        credentialId: "new-cred-1",
        scopes: [],
        agentId: "new-agent-id",
      }),
    );
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

describe("NewAgentDialog: the department's threshold on an inherited tool", () => {
  async function hireWithFrameThreshold(hasValueSpec: boolean) {
    connectionsMock.mockReturnValue({ data: [{ ...connections[0], hasValueSpec }] });
    departmentToolsMock.mockReturnValue({
      data: { tools: { Odoo: { enabled: true, read: true, modify: true, approval_eur: 200 } } },
    });
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("button", { name: /Next/i })); // -> Guardrails & Trigger
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(createAgentMock).toHaveBeenCalled());
    return createAgentMock.mock.calls[0][0].narrowing.tools.Odoo;
  }

  it("leaves it out for a connection with no value to compare it against", async () => {
    // The frame's threshold applies anyway; a copy saved on the agent went
    // stale the moment the department changed its value (E2E F3 review).
    expect(await hireWithFrameThreshold(false)).not.toHaveProperty("approval_eur");
  });

  it("keeps it for a connection with a value spec", async () => {
    expect(await hireWithFrameThreshold(true)).toMatchObject({ approval_eur: 200 });
  });
});

describe("NewAgentDialog: an agent-only grant from a preset with a threshold", () => {
  it("leaves a positive threshold out on a connection without a value spec", async () => {
    // The backend refuses a NEW positive threshold there (it never fires: the
    // connection reports no amount), so sending the preset's €100 would turn
    // a plain hire into a 422.
    connectionsMock.mockReturnValue({
      data: [
        {
          ...connections[0],
          hasValueSpec: false,
          guardrailPresets: [
            {
              key: "careful",
              label: "Careful",
              recommended: true,
              read: true,
              modify: true,
              approvalActions: [],
              approvalEur: 100,
              only: null,
              limits: [],
            },
          ],
        },
      ],
    });
    departmentToolsMock.mockReturnValue({ data: { tools: {} } }); // agent-only grant
    renderDialog();
    await screen.findByText("Hire a new agent");
    fillIdentityAndGoToTools();
    fireEvent.click(screen.getByRole("button", { name: "Add tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("button", { name: /Next/i }));
    fireEvent.click(screen.getByRole("button", { name: /Hire agent/i }));
    await waitFor(() => expect(setNarrowingMock).toHaveBeenCalled());
    const granted = setNarrowingMock.mock.calls[0][0].narrowing.tools.Odoo;
    expect(granted).toMatchObject({ enabled: true, modify: true });
    expect(granted).not.toHaveProperty("approval_eur");
  });
});
