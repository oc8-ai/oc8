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
      expect(de).not.toMatch(/\b(du|dein)\b/i);
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
