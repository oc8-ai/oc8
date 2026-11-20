import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  CopilotSettingsPanel,
  MailServerSetting,
  OrganizationPanel,
  TwoFactorPolicyPanel,
} from "@/routes/settings";
vi.mock("@/lib/governance-hooks", () => ({ useMay: () => () => true }));
import type { CredentialDTO, OrganizationSettingsDTO } from "@/lib/hooks";

// vi.hoisted: `vi.mock` factories are hoisted above ordinary `const`s by
// vitest -- see the identical pattern in profile.test.tsx/backup-panel.test.tsx.
const {
  getOrgMock,
  putOrgMock,
  getCredentialsMock,
  testCredentialMock,
  getCopilotMock,
  putCopilotMock,
  getTwoFactorMock,
  putTwoFactorMock,
} = vi.hoisted(() => ({
  getTwoFactorMock: vi.fn(),
  putTwoFactorMock: vi.fn(),
  getCopilotMock: vi.fn(),
  putCopilotMock: vi.fn(),
  getOrgMock: vi.fn(),
  putOrgMock: vi.fn(),
  getCredentialsMock: vi.fn(),
  testCredentialMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    get: (path: string) => {
      if (path === "/settings/organization") return getOrgMock();
      if (path === "/settings/copilot") return getCopilotMock();
      if (path === "/settings/organization/two-factor") return getTwoFactorMock();
      if (path.startsWith("/credentials?type=")) return getCredentialsMock();
      // <CredentialPicker>'s own useCredentialTypes() call -- unused by these
      // tests (none of them open its inline "Create new" form), but it must
      // resolve to something or that query rejects and spams the console.
      if (path === "/credential-types") return Promise.resolve([]);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    },
    post: (path: string, body: unknown) => {
      const match = /^\/credentials\/([^/]+)\/test$/.exec(path);
      if (match) return testCredentialMock(match[1], body);
      return Promise.reject(new Error(`unexpected POST ${path}`));
    },
    put: (path: string, body: unknown) => {
      if (path === "/settings/organization") return putOrgMock(body);
      if (path === "/settings/copilot") return putCopilotMock(body);
      if (path === "/settings/organization/two-factor") return putTwoFactorMock(body);
      return Promise.reject(new Error(`unexpected PUT ${path}`));
    },
  },
}));

const { toastErrorMock, toastSuccessMock } = vi.hoisted(() => ({
  toastErrorMock: vi.fn(),
  toastSuccessMock: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof import("sonner")>();
  return {
    ...actual,
    toast: { ...actual.toast, error: toastErrorMock, success: toastSuccessMock },
  };
});

const ORG: OrganizationSettingsDTO = {
  id: "org-1",
  name: "Acme GmbH",
  slug: "acme",
  tier: "pro",
  region: "eu",
  active_smtp_credential_id: null,
};

const SENDGRID: CredentialDTO = {
  id: "cred-1",
  name: "SendGrid",
  credentialType: "smtp_server",
  fieldValues: {},
  lastTestedAt: null,
  lastTestOk: null,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
};

const MAILGUN: CredentialDTO = { ...SENDGRID, id: "cred-2", name: "Mailgun" };

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MailServerSetting />
    </QueryClientProvider>,
  );
}

function credentialList() {
  return screen.findByRole("list", { name: /mail server credentials/i });
}

describe("MailServerSetting", () => {
  beforeEach(() => {
    getOrgMock.mockReset();
    putOrgMock.mockReset();
    getCredentialsMock.mockReset();
    testCredentialMock.mockReset();
    toastErrorMock.mockReset();
    toastSuccessMock.mockReset();
  });

  it("renders the tenant's current smtp_server credentials", async () => {
    getOrgMock.mockResolvedValue(ORG);
    getCredentialsMock.mockResolvedValue([SENDGRID, MAILGUN]);
    renderPanel();

    const list = await screen.findByRole("list", { name: /mail server credentials/i });
    expect(within(list).getByText("SendGrid")).toBeInTheDocument();
    expect(within(list).getByText("Mailgun")).toBeInTheDocument();
    // Fetched with the credentials-list endpoint's own type filter, not a
    // client-side filter over every credential the tenant owns.
    expect(getCredentialsMock).toHaveBeenCalled();
  });

  it("does not render a credential list when the tenant has none yet", async () => {
    getOrgMock.mockResolvedValue(ORG);
    getCredentialsMock.mockResolvedValue([]);
    renderPanel();

    await screen.findByText(/mail server/i);
    expect(
      screen.queryByRole("list", { name: /mail server credentials/i }),
    ).not.toBeInTheDocument();
  });

  it("selecting a credential calls PUT /settings/organization with active_smtp_credential_id set to that id", async () => {
    getOrgMock.mockResolvedValue(ORG);
    getCredentialsMock.mockResolvedValue([SENDGRID, MAILGUN]);
    putOrgMock.mockResolvedValue({ ...ORG, active_smtp_credential_id: SENDGRID.id });
    renderPanel();

    const select = await screen.findByRole("combobox");
    // The <select>'s own <option>s populate asynchronously (useCredentials'
    // fetch) -- firing change before they land would set a value with no
    // matching <option>, which jsdom silently ignores (the underlying DOM
    // value stays ""), making this assert against a phantom "none" selection.
    await screen.findByRole("option", { name: SENDGRID.name });
    fireEvent.change(select, { target: { value: SENDGRID.id } });

    await waitFor(() =>
      expect(putOrgMock).toHaveBeenCalledWith({
        name: ORG.name,
        region: ORG.region,
        active_smtp_credential_id: SENDGRID.id,
      }),
    );
    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalled());
  });

  it('"None (disable)" calls PUT /settings/organization with active_smtp_credential_id: null', async () => {
    getOrgMock.mockResolvedValue({ ...ORG, active_smtp_credential_id: SENDGRID.id });
    getCredentialsMock.mockResolvedValue([SENDGRID, MAILGUN]);
    putOrgMock.mockResolvedValue({ ...ORG, active_smtp_credential_id: null });
    renderPanel();

    const button = await screen.findByRole("button", { name: /none \(disable\)/i });
    fireEvent.click(button);

    await waitFor(() =>
      expect(putOrgMock).toHaveBeenCalledWith({
        name: ORG.name,
        region: ORG.region,
        active_smtp_credential_id: null,
      }),
    );
  });

  it('does not offer "None (disable)" when no credential is currently active', async () => {
    getOrgMock.mockResolvedValue(ORG);
    getCredentialsMock.mockResolvedValue([SENDGRID]);
    renderPanel();

    await screen.findByRole("combobox");
    expect(screen.queryByRole("button", { name: /none \(disable\)/i })).not.toBeInTheDocument();
  });

  it('"Test" calls the existing per-credential test endpoint for that credential', async () => {
    getOrgMock.mockResolvedValue({ ...ORG, active_smtp_credential_id: SENDGRID.id });
    getCredentialsMock.mockResolvedValue([SENDGRID, MAILGUN]);
    testCredentialMock.mockResolvedValue({ ok: true });
    renderPanel();

    const list = await credentialList();
    const mailgunRow = within(list).getByText("Mailgun").closest("li")!;
    fireEvent.click(within(mailgunRow).getByRole("button", { name: /test/i }));

    await waitFor(() => expect(testCredentialMock).toHaveBeenCalledWith(MAILGUN.id, {}));
    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalled());
  });

  it("shows a real error message when a test fails", async () => {
    getOrgMock.mockResolvedValue({ ...ORG, active_smtp_credential_id: SENDGRID.id });
    getCredentialsMock.mockResolvedValue([SENDGRID]);
    testCredentialMock.mockRejectedValue(new Error("connection refused"));
    renderPanel();

    const list = await credentialList();
    const row = within(list).getByText("SendGrid").closest("li")!;
    fireEvent.click(within(row).getByRole("button", { name: /test/i }));

    await waitFor(() =>
      expect(toastErrorMock).toHaveBeenCalledWith(
        expect.stringMatching(/test failed/i),
        expect.objectContaining({ description: "connection refused" }),
      ),
    );
  });
});

describe("CopilotSettingsPanel", () => {
  it("shows the cap and PUTs a new one", async () => {
    getCopilotMock.mockResolvedValue({ maxActiveFollowups: 20 });
    putCopilotMock.mockResolvedValue({ maxActiveFollowups: 7 });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <CopilotSettingsPanel />
      </QueryClientProvider>,
    );
    const input = await screen.findByLabelText(/max\. active follow-ups per person/i);
    await waitFor(() => expect(input).toHaveValue(20));
    fireEvent.change(input, { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(putCopilotMock).toHaveBeenCalledWith({ maxActiveFollowups: 7 }));
  });
});

describe("OrganizationPanel timezone", () => {
  it("shows the organisation's timezone, PUTs a new one and refreshes /me", async () => {
    getOrgMock.mockResolvedValue({ ...ORG, timezone: "UTC" });
    putOrgMock.mockReset();
    putOrgMock.mockResolvedValue({ ...ORG, timezone: "Europe/Berlin" });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    render(
      <QueryClientProvider client={qc}>
        <OrganizationPanel />
      </QueryClientProvider>,
    );
    const zone = (await screen.findByLabelText("Timezone")) as HTMLInputElement;
    await waitFor(() => expect(zone.value).toBe("UTC"));
    fireEvent.change(zone, { target: { value: "Europe/Berlin" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(putOrgMock).toHaveBeenCalledWith({
        name: ORG.name,
        region: ORG.region,
        timezone: "Europe/Berlin",
      }),
    );
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["auth", "me"] }));
  });
});

describe("TwoFactorPolicyPanel", () => {
  const POLICY = {
    requirement: "admins",
    methods: ["totp", "email", "passkey"],
    graceDays: 7,
    emailAvailable: true,
    passkeyAvailable: true,
  };

  function renderPanel() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <TwoFactorPolicyPanel />
      </QueryClientProvider>,
    );
  }

  beforeEach(() => {
    getTwoFactorMock.mockReset();
    putTwoFactorMock.mockReset();
  });

  it("saves the requirement, methods and grace days", async () => {
    getTwoFactorMock.mockResolvedValue(POLICY);
    putTwoFactorMock.mockResolvedValue({ ...POLICY, requirement: "everyone", graceDays: 3 });
    renderPanel();

    fireEvent.click(await screen.findByRole("radio", { name: /everyone/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /code by email/i }));
    fireEvent.change(screen.getByLabelText(/grace period \(days\)/i), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(putTwoFactorMock).toHaveBeenCalledWith({
        requirement: "everyone",
        methods: ["totp", "passkey"],
        graceDays: 3,
      }),
    );
  });

  it("warns before lowering the requirement to optional", async () => {
    getTwoFactorMock.mockResolvedValue(POLICY);
    renderPanel();

    fireEvent.click(await screen.findByRole("radio", { name: /optional/i }));

    expect(screen.getByText(/no longer be asked/i)).toBeInTheDocument();
  });

  it("shows the server's refusal as text", async () => {
    getTwoFactorMock.mockResolvedValue(POLICY);
    putTwoFactorMock.mockRejectedValue(
      new Error("This policy would require you to use a second factor you have not set up."),
    );
    renderPanel();

    fireEvent.click(await screen.findByRole("radio", { name: /everyone/i }));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    expect(
      await screen.findByText(/would require you to use a second factor you have not set up/i),
    ).toBeInTheDocument();
  });

  it("disables email with the reason when outgoing mail is missing", async () => {
    getTwoFactorMock.mockResolvedValue({
      ...POLICY,
      methods: ["totp"],
      emailAvailable: false,
    });
    renderPanel();

    expect(await screen.findByRole("checkbox", { name: /code by email/i })).toBeDisabled();
    expect(screen.getByText(/needs a mail server/i)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /passkey/i })).toBeEnabled();
  });

  it("enables passkeys when the public URL can carry them", async () => {
    getTwoFactorMock.mockResolvedValue({ ...POLICY, methods: ["totp"] });
    putTwoFactorMock.mockResolvedValue(POLICY);
    renderPanel();

    const passkey = await screen.findByRole("checkbox", { name: /passkey/i });
    expect(passkey).toBeEnabled();
    expect(passkey).not.toBeChecked();
    expect(screen.queryByText(/coming soon/i)).toBeNull();
    fireEvent.click(passkey);
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(putTwoFactorMock).toHaveBeenCalledWith({
        requirement: "admins",
        methods: ["totp", "passkey"],
        graceDays: 7,
      }),
    );
  });

  it("disables passkeys with the reason when the public URL cannot carry them", async () => {
    getTwoFactorMock.mockResolvedValue({ ...POLICY, methods: ["totp"], passkeyAvailable: false });
    renderPanel();

    expect(await screen.findByRole("checkbox", { name: /passkey/i })).toBeDisabled();
    expect(screen.getByText(/needs https/i)).toBeInTheDocument();
  });

  it("leaves out the methods this server cannot offer, so saving the default policy works", async () => {
    getTwoFactorMock.mockResolvedValue({
      ...POLICY,
      emailAvailable: false,
      passkeyAvailable: false,
    });
    putTwoFactorMock.mockResolvedValue({ ...POLICY, requirement: "everyone", methods: ["totp"] });
    renderPanel();

    const passkey = await screen.findByRole("checkbox", { name: /passkey/i });
    const email = screen.getByRole("checkbox", { name: /code by email/i });
    expect(passkey).not.toBeChecked();
    expect(passkey).toBeDisabled();
    expect(email).not.toBeChecked();
    expect(email).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: /everyone/i }));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(putTwoFactorMock).toHaveBeenCalledWith({
        requirement: "everyone",
        methods: ["totp"],
        graceDays: 7,
      }),
    );
  });
});
