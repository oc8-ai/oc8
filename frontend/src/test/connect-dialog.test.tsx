import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReturnError } from "@/components/connections/return-error";
import { returnErrorText } from "@/components/connections/texts";
import { OAUTH_ERROR_CODES } from "@/lib/connections";
import { LanguageProvider } from "@/lib/i18n";

const integrations = vi.fn();
const start = vi.fn();
const check = vi.fn();
const create = vi.fn();
const may = vi.fn();
const capaIcon = vi.fn();
const toastError = vi.fn();

vi.mock("sonner", () => ({ toast: { error: (m: string) => toastError(m), success: vi.fn() } }));
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return { ...actual, useCapaIcon: (id: string) => capaIcon(id) };
});

vi.mock("@/lib/connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/connections")>();
  return {
    ...actual,
    useIntegrations: () => integrations(),
    useStartOAuth: () => ({ mutateAsync: start, isPending: false }),
    useCheckCredential: () => ({ mutateAsync: check, isPending: false }),
    useCreateCredentialConnection: () => ({ mutateAsync: create, isPending: false }),
    useUpdateOrganisationCredential: () => ({ mutateAsync: vi.fn(), isPending: false }),
  };
});
vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useMay: () => may };
});

import { ConnectDialog } from "@/components/connections/connect-dialog";

const MS = {
  name: "microsoft365",
  label: "Microsoft 365",
  summary: "",
  toolKey: "microsoft365",
  kind: "oauth",
  provider: "microsoft",
  credentialType: null,
  credentialFields: [],
  available: true,
  capaId: "c1",
};
const ODOO = {
  name: "odoo_mcp",
  label: "Odoo",
  summary: "",
  toolKey: "odoo",
  kind: "credential",
  provider: null,
  credentialType: "odoo_login",
  available: true,
  capaId: "c2",
  credentialFields: [
    { key: "url", label: "Odoo URL", kind: "url" },
    { key: "username", label: "Odoo user (login)", kind: "text" },
    {
      key: "password",
      label: "Odoo password or API key",
      kind: "password",
      label_translations: { de: "Odoo-Passwort oder API-Schlüssel" },
    },
  ],
};

function renderDialog(integration: string, redirect = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <LanguageProvider>
        <ConnectDialog integration={integration} open onOpenChange={vi.fn()} redirect={redirect} />
      </LanguageProvider>
    </QueryClientProvider>,
  );
  return redirect;
}

describe("ConnectDialog", () => {
  beforeEach(() => {
    integrations.mockReturnValue({ data: [MS, ODOO], isLoading: false });
    may.mockReturnValue(false);
    capaIcon.mockReturnValue({ data: null });
    toastError.mockReset();
    start.mockReset();
    check.mockReset();
    create.mockReset();
  });
  afterEach(() => window.localStorage.removeItem("oc8-lang"));

  it("signs a member in with Microsoft for themselves, in German", async () => {
    window.localStorage.setItem("oc8-lang", "de");
    start.mockResolvedValue({ authorizationUrl: "https://login.microsoftonline.com/x" });
    const redirect = renderDialog("microsoft365");
    // German appears after the language provider's mount effect: findBy first.
    expect(await screen.findByText("Microsoft 365 verbinden")).toBeInTheDocument();
    expect(screen.getByText(/E-Mails in Outlook lesen, sortieren und senden/)).toBeInTheDocument();
    expect(screen.queryByText("Für die Organisation")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mit Microsoft anmelden" }));
    await waitFor(() =>
      expect(redirect).toHaveBeenCalledWith("https://login.microsoftonline.com/x"),
    );
    expect(start).toHaveBeenCalledWith({ provider: "microsoft", owner: "me" });
  });

  it("lets an admin connect for the organisation", async () => {
    may.mockReturnValue(true);
    start.mockResolvedValue({ authorizationUrl: "https://x" });
    renderDialog("microsoft365");
    fireEvent.click(screen.getByLabelText("For the organisation"));
    fireEvent.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    await waitFor(() =>
      expect(start).toHaveBeenCalledWith({ provider: "microsoft", owner: "organisation" }),
    );
  });

  it("explains where a value is and turns a wrong login into words", async () => {
    window.localStorage.setItem("oc8-lang", "de");
    check.mockResolvedValue({ ok: false, reason: "auth", detail: "Access Denied" });
    renderDialog("odoo_mcp");
    fireEvent.click((await screen.findAllByText("Wo finde ich das?"))[2]);
    expect(
      screen.getByText(/Mein Profil → Kontosicherheit → Neuer API-Schlüssel/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Odoo-Passwort oder API-Schlüssel"), {
      target: { value: "nope" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Verbindung prüfen" }));
    expect(
      await screen.findByText("Benutzername oder API-Schlüssel stimmen nicht."),
    ).toBeInTheDocument();
    expect(check).toHaveBeenCalledWith({
      integration: "odoo_mcp",
      fieldValues: expect.objectContaining({ password: "nope" }),
    });
  });

  it("saves a member's own login", async () => {
    create.mockResolvedValue({});
    renderDialog("odoo_mcp");
    fireEvent.change(screen.getByLabelText("Odoo URL"), {
      target: { value: "https://odoo.acme.de" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        integration: "odoo_mcp",
        owner: "me",
        fieldValues: expect.objectContaining({ url: "https://odoo.acme.de" }),
      }),
    );
  });
});

describe("returnErrorText", () => {
  it("has a distinct German and English text for every error code", () => {
    for (const code of OAUTH_ERROR_CODES) {
      const en = returnErrorText(code, (e) => e);
      const de = returnErrorText(code, (_e, d) => d);
      expect(en.length).toBeGreaterThan(10);
      expect(de.length).toBeGreaterThan(10);
      expect(de).not.toMatch(/\b(Sie|Ihr|Ihre|Ihren|Ihrem|Ihrer|Ihnen)\b/);
    }
    expect(returnErrorText("access_denied", (e) => e)).not.toBe(
      returnErrorText("tenant_mismatch", (e) => e),
    );
  });

  it("shows the admin-consent link for the IT administrator", () => {
    render(
      <LanguageProvider>
        <ReturnError
          code="admin_consent_required"
          adminConsentUrl="https://login.microsoftonline.com/consent"
        />
      </LanguageProvider>,
    );
    expect(
      screen.getByDisplayValue("https://login.microsoftonline.com/consent"),
    ).toBeInTheDocument();
  });
});

describe("ConnectDialog fix round 1", () => {
  beforeEach(() => {
    integrations.mockReturnValue({ data: [MS, ODOO], isLoading: false });
    may.mockReturnValue(false);
    capaIcon.mockReturnValue({ data: null });
    toastError.mockReset();
    start.mockReset();
    create.mockReset();
    check.mockReset();
  });

  it("never prints the raw check detail", async () => {
    check.mockResolvedValue({ ok: false, reason: "invalid", detail: "SECRET-BACKEND-TEXT" });
    renderDialog("odoo_mcp");
    fireEvent.click(await screen.findByRole("button", { name: "Check connection" }));
    await screen.findByRole("status");
    expect(screen.queryByText(/SECRET-BACKEND-TEXT/)).not.toBeInTheDocument();
  });

  it("does not toast raw error messages", async () => {
    create.mockRejectedValue(new Error("SECRET-BACKEND-TEXT"));
    renderDialog("odoo_mcp");
    fireEvent.click(await screen.findByRole("button", { name: "Save" }));
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError.mock.calls[0][0]).not.toMatch(/SECRET-BACKEND-TEXT/);
  });

  it("falls back to 'me' when the permission disappears", async () => {
    may.mockReturnValue(true);
    start.mockResolvedValue({ authorizationUrl: "https://x" });
    const qc = new QueryClient();
    const ui = () => (
      <QueryClientProvider client={qc}>
        <LanguageProvider>
          <ConnectDialog
            integration="microsoft365"
            open
            onOpenChange={vi.fn()}
            defaultOwner="organisation"
            redirect={vi.fn()}
          />
        </LanguageProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(ui());
    may.mockReturnValue(false);
    rerender(ui());
    fireEvent.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    await waitFor(() => expect(start).toHaveBeenCalledWith({ provider: "microsoft", owner: "me" }));
  });

  it("honours defaultOwner when permissions load after mount", async () => {
    start.mockResolvedValue({ authorizationUrl: "https://x" });
    const qc = new QueryClient();
    const ui = () => (
      <QueryClientProvider client={qc}>
        <LanguageProvider>
          <ConnectDialog
            integration="microsoft365"
            open
            onOpenChange={vi.fn()}
            defaultOwner="organisation"
            redirect={vi.fn()}
          />
        </LanguageProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(ui());
    may.mockReturnValue(true);
    rerender(ui());
    fireEvent.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    await waitFor(() =>
      expect(start).toHaveBeenCalledWith({ provider: "microsoft", owner: "organisation" }),
    );
  });

  it("shows the capa's own icon when it has one", () => {
    capaIcon.mockReturnValue({ data: "blob:icon" });
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <LanguageProvider>
          <ConnectDialog integration="microsoft365" open onOpenChange={vi.fn()} />
        </LanguageProvider>
      </QueryClientProvider>,
    );
    expect(capaIcon).toHaveBeenCalledWith("c1");
    expect(document.body.querySelector('img[src="blob:icon"]')).not.toBeNull();
    expect(container).toBeTruthy();
  });

  it("has a localized description and a real error state", async () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderDialog("microsoft365");
    expect(await screen.findByText("E-Mails, Kalender und Dateien")).toBeInTheDocument();
    window.localStorage.removeItem("oc8-lang");
  });

  it("shows an error, not 'not available', when loading fails", () => {
    integrations.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    renderDialog("microsoft365");
    expect(screen.queryByText(/not available yet/)).not.toBeInTheDocument();
    expect(screen.getByText(/could not be loaded/)).toBeInTheDocument();
  });
});
