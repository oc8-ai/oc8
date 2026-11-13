import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/lib/i18n";

const integrations = vi.fn();
const mine = vi.fn();
const org = vi.fn();
const members = vi.fn();
const orgEnabled = vi.fn();
const membersEnabled = vi.fn();
const disconnect = vi.fn();
const complete = vi.fn();
const consentUrl = vi.fn();
const may = vi.fn();
const capaIcon = vi.fn();
const enable = vi.fn();

vi.mock("@/lib/connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/connections")>();
  return {
    ...actual,
    useIntegrations: () => integrations(),
    useMyConnections: () => mine(),
    useOrganisationConnections: (enabled: boolean) => {
      orgEnabled(enabled);
      return enabled ? org() : { data: undefined, isLoading: false };
    },
    useMemberConnections: (enabled: boolean) => {
      membersEnabled(enabled);
      return enabled ? members() : { data: undefined, isLoading: false };
    },
    useDisconnect: () => ({ mutateAsync: disconnect, isPending: false }),
    useTestConnection: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useCompleteOAuth: () => ({ mutateAsync: complete, isPending: false }),
    useAdminConsentUrl: (enabled: boolean) => (enabled ? consentUrl() : { data: undefined }),
  };
});
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useCapaIcon: (id: string) => capaIcon(id),
    useEnablePlugin: () => ({ mutateAsync: enable, isPending: false }),
  };
});
vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useMay: () => may };
});
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({
      to,
      children,
      className,
    }: {
      to: string;
      children: React.ReactNode;
      className?: string;
    }) => (
      <a href={to} className={className} data-router-link="">
        {children}
      </a>
    ),
  };
});
vi.mock("@/components/connections/organisation-credentials", () => ({
  OrganisationCredentials: () => <div>further-credentials</div>,
}));
vi.mock("@/components/custom-mcp-wizard", () => ({
  CustomMcpWizard: ({ open }: { open: boolean }) =>
    open ? <div role="dialog">custom-wizard</div> : null,
}));
vi.mock("@/components/connections/connect-dialog", () => ({
  ConnectDialog: ({ integration, open }: { integration: string; open: boolean }) =>
    open ? <div role="dialog">dialog:{integration}</div> : null,
}));

import {
  ConnectionsPage,
  type ConnectionsSearch,
  type ConnectionsTab,
} from "@/components/connections/connections-page";
import { OAuthCompleteError } from "@/lib/connections";

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
  credentialFields: [],
  available: true,
  capaId: "c2",
};
const row = (over: Record<string, unknown>) => ({
  id: "r1",
  kind: "oauth",
  integration: "microsoft365",
  provider: "microsoft",
  credentialType: null,
  accountLabel: "anna@acme.de",
  owner: "me",
  ownerMemberId: "m1",
  ownerName: null,
  status: "active",
  grantType: "authorization_code",
  createdAt: "2026-11-08T10:00:00Z",
  createdByName: null,
  lastTestedAt: null,
  lastTestOk: null,
  ...over,
});

function renderPage(search: ConnectionsSearch = {}, onConsume = vi.fn(), strict = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let current = search;
  // Stands in for the route: a chosen tab lands in the search, like the URL.
  const onTabChange = vi.fn((tab: ConnectionsTab) => setSearch({ ...current, tab }));
  const tree = (next: ConnectionsSearch) => {
    const page = (
      <ConnectionsPage search={next} onConsumeSearch={onConsume} onTabChange={onTabChange} />
    );
    // StrictMode wraps the page only: LanguageProvider's own mount effects
    // would otherwise reset the stored language on their second run.
    return (
      <QueryClientProvider client={qc}>
        <LanguageProvider>{strict ? <StrictMode>{page}</StrictMode> : page}</LanguageProvider>
      </QueryClientProvider>
    );
  };
  const view = render(tree(search));
  function setSearch(next: ConnectionsSearch) {
    current = next;
    view.rerender(tree(next));
  }
  return { onConsume, onTabChange, setSearch, ...view };
}

// Radix menus open on pointerdown, which fireEvent.click does not send.
function openMenu(name: string | RegExp) {
  fireEvent.pointerDown(screen.getByRole("button", { name }), { button: 0 });
}

const ADMIN = (p: string) => p === "integration:manage";

describe("ConnectionsPage", () => {
  beforeEach(() => {
    window.localStorage.setItem("oc8-lang", "de");
    integrations.mockReturnValue({ data: [MS, ODOO], isLoading: false });
    mine.mockReturnValue({ data: [row({})], isLoading: false });
    org.mockReturnValue({
      data: [
        row({
          id: "o1",
          owner: "organisation",
          accountLabel: "Odoo system",
          kind: "credential",
          integration: "odoo_mcp",
          status: "ok",
        }),
      ],
      isLoading: false,
    });
    members.mockReturnValue({
      data: [row({ id: "b1", owner: "member", ownerName: "Ben", accountLabel: "ben@acme.de" })],
      isLoading: false,
    });
    consentUrl.mockReturnValue({
      data: {
        url: "https://login.microsoftonline.com/organizations/v2.0/adminconsent?client_id=x",
      },
    });
    capaIcon.mockReturnValue({ data: null });
    may.mockReturnValue(false);
    orgEnabled.mockReset();
    membersEnabled.mockReset();
    disconnect.mockReset();
    complete.mockReset();
    enable.mockReset();
    enable.mockResolvedValue({});
  });

  it("offers the custom server tile to plugin managers and opens the wizard", () => {
    may.mockReturnValue(true);
    renderPage();
    expect(screen.getByText("Eigener Server oder eigene API")).toBeTruthy();
    expect(screen.getByText("Verbinde deinen eigenen MCP-Server oder eine REST-API.")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Eigenen Server oder eigene API hinzufügen" }),
    );
    expect(screen.getByText("custom-wizard")).toBeTruthy();
  });

  it("hides the custom server tile from people who may not manage plugins", () => {
    may.mockReturnValue(false);
    renderPage();
    expect(screen.queryByText("Eigener Server oder eigene API")).toBeNull();
  });

  it("lets an admin make a legacy installed integration available", async () => {
    may.mockReturnValue(true);
    integrations.mockReturnValue({
      data: [{ ...ODOO, available: false, permissions: ["mcp:connect"] }],
      isLoading: false,
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Odoo bereitstellen" }));
    await waitFor(() =>
      expect(enable).toHaveBeenCalledWith({ pluginId: "c2", grantedPermissions: ["mcp:connect"] }),
    );
  });

  it("offers making it available only to whoever may enable plugins", async () => {
    // Manages connections, but enabling a plugin needs plugin:manage.
    may.mockImplementation((p: string) => p === "integration:manage");
    integrations.mockReturnValue({
      data: [{ ...ODOO, available: false, permissions: ["mcp:connect"] }],
      isLoading: false,
    });
    renderPage();
    await screen.findByRole("tab", { name: "Organisation" });
    expect(screen.queryByRole("button", { name: "Odoo bereitstellen" })).not.toBeInTheDocument();
  });

  it("shows a member only their own connections, without tabs", async () => {
    renderPage({ tab: "members" });
    const mineSection = await screen.findByRole("region", { name: "Meine Verbindungen" });
    expect(within(mineSection).getByText("anna@acme.de · seit 8. Nov.")).toBeInTheDocument();
    expect(within(mineSection).getByText("Nicht verbunden")).toBeInTheDocument();
    expect(within(mineSection).getByRole("button", { name: "Odoo verbinden" })).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByText("Ben")).not.toBeInTheDocument();
    // The admin-only lists are never even fetched for a member.
    expect(orgEnabled).toHaveBeenCalledWith(false);
    expect(orgEnabled).not.toHaveBeenCalledWith(true);
    expect(membersEnabled).not.toHaveBeenCalledWith(true);
  });

  it("gives admins three tabs, the member count on the last", async () => {
    may.mockImplementation(ADMIN);
    renderPage();
    const tabs = within(await screen.findByRole("tablist")).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Meine", "Organisation", "Mitglieder (1)"]);
    expect(screen.getByRole("tab", { name: "Meine" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: "Meine" })).toHaveTextContent("Microsoft 365");
    expect(orgEnabled).toHaveBeenCalledWith(true);
    expect(membersEnabled).toHaveBeenCalledWith(true);
  });

  it("opens the tab the URL names and writes a chosen tab back", () => {
    may.mockImplementation(ADMIN);
    const { onTabChange } = renderPage({ tab: "organisation" });
    const orgPanel = screen.getByRole("tabpanel", { name: "Organisation" });
    expect(within(orgPanel).getByText(/^Odoo system ·/)).toBeInTheDocument();
    expect(screen.getByText("further-credentials")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Mitglieder (1)" }));
    expect(onTabChange).toHaveBeenCalledWith("members");
    expect(
      within(screen.getByRole("tabpanel", { name: "Mitglieder (1)" })).getByText("Ben"),
    ).toBeInTheDocument();
  });

  it("marks a personal login an organisation access already covers", () => {
    may.mockImplementation(ADMIN);
    renderPage();
    const panel = screen.getByRole("tabpanel", { name: "Meine" });
    expect(within(panel).getByText("Organisationszugang aktiv")).toBeInTheDocument();
  });

  it("asks to reconnect a broken connection and opens its assistant", () => {
    mine.mockReturnValue({ data: [row({ status: "needs_reauth" })], isLoading: false });
    renderPage();
    expect(screen.getByText("anna@acme.de · Erneut verbinden nötig")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Erneut verbinden" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("dialog:microsoft365");
  });

  it("asks before disconnecting the caller's own account", async () => {
    disconnect.mockResolvedValue(undefined);
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "anna@acme.de trennen" }));
    const confirmBox = await screen.findByRole("alertdialog");
    fireEvent.click(within(confirmBox).getByRole("button", { name: "Trennen" }));
    await waitFor(() =>
      expect(disconnect).toHaveBeenCalledWith(expect.objectContaining({ id: "r1" })),
    );
  });

  it("adds an organisation access for the chosen app", async () => {
    may.mockImplementation(ADMIN);
    renderPage({ tab: "organisation" });
    openMenu("Zugang hinzufügen");
    fireEvent.click(await screen.findByRole("menuitem", { name: "Odoo" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("dialog:odoo_mcp");
  });

  it("asks before revoking a member's connection", async () => {
    may.mockImplementation(ADMIN);
    disconnect.mockResolvedValue(undefined);
    renderPage({ tab: "members" });
    openMenu("Aktionen für Ben (ben@acme.de)");
    fireEvent.click(await screen.findByRole("menuitem", { name: "Widerrufen" }));
    const confirmBox = await screen.findByRole("alertdialog");
    expect(confirmBox).toHaveTextContent("Ben verliert diese Verbindung (ben@acme.de)");
    expect(disconnect).not.toHaveBeenCalled();
    fireEvent.click(within(confirmBox).getByRole("button", { name: "Widerrufen" }));
    await waitFor(() =>
      expect(disconnect).toHaveBeenCalledWith(expect.objectContaining({ id: "b1" })),
    );
  });

  it("shows the logo in a row when the integration has one, the name alone otherwise", () => {
    capaIcon.mockImplementation((id: string) => ({ data: id === "c1" ? "blob:ms-logo" : null }));
    renderPage();
    const mineSection = screen.getByRole("region", { name: "Meine Verbindungen" });
    const logos = within(mineSection).getAllByRole("presentation", { hidden: true });
    expect(logos).toHaveLength(1);
    expect(logos[0]).toHaveAttribute("src", "blob:ms-logo");
    expect(within(mineSection).getByText("Odoo")).toBeInTheDocument();
  });

  it("completes the sign-in once, even under StrictMode, and keeps the result", async () => {
    let resolve!: (c: unknown) => void;
    complete.mockReturnValue(new Promise((r) => (resolve = r)));
    const { onConsume } = renderPage({ pending: "p-123", provider: "microsoft" }, vi.fn(), true);
    expect(onConsume).toHaveBeenCalledTimes(1);
    expect(complete).toHaveBeenCalledTimes(1);
    expect(complete).toHaveBeenCalledWith({ provider: "microsoft", pending: "p-123" });
    await act(async () => resolve(row({ accountLabel: "carla@acme.de" })));
    expect(await screen.findByRole("status")).toHaveTextContent("Verbunden als carla@acme.de");
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it("drops the pending id from the URL before completing", () => {
    const order: string[] = [];
    complete.mockImplementation(() => {
      order.push("complete");
      return new Promise(() => {});
    });
    renderPage(
      { pending: "p-123", provider: "microsoft" },
      vi.fn(() => order.push("consume")),
    );
    expect(order).toEqual(["consume", "complete"]);
  });

  it("shows the copyable approval link when completing needs an administrator", async () => {
    complete.mockRejectedValue(
      new OAuthCompleteError(
        "admin_consent_required",
        "https://login.microsoftonline.com/x/adminconsent?client_id=y",
      ),
    );
    renderPage({ pending: "p-1", provider: "microsoft" });
    expect(await screen.findByDisplayValue(/adminconsent\?client_id=y/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Dein IT-Administrator muss diese Verbindung zuerst freigeben",
    );
  });

  it("explains other completion failures in plain words", async () => {
    complete.mockRejectedValue(new OAuthCompleteError("tenant_mismatch"));
    renderPage({ pending: "p-1", provider: "microsoft" });
    expect(await screen.findByRole("alert")).toHaveTextContent("anderen Organisation");
  });

  it("gives a copyable admin consent link when the return says so", () => {
    const { onConsume } = renderPage({ error: "admin_consent_required", provider: "microsoft" });
    expect(screen.getByDisplayValue(/adminconsent\?client_id=x/)).toBeInTheDocument();
    expect(onConsume).toHaveBeenCalledTimes(1);
    expect(complete).not.toHaveBeenCalled();
  });

  it("explains an error the return reported", () => {
    renderPage({ error: "access_denied", provider: "google" });
    expect(screen.getByRole("alert")).toHaveTextContent("Du hast die Anmeldung abgebrochen");
  });

  it("sends no referrer while the page is open", () => {
    complete.mockReturnValue(new Promise(() => {}));
    const { unmount } = renderPage({ pending: "p-1", provider: "microsoft" });
    const meta = document.head.querySelector('meta[name="referrer"]');
    expect(meta).toHaveAttribute("content", "no-referrer");
    unmount();
    expect(document.head.querySelector('meta[name="referrer"][content="no-referrer"]')).toBeNull();
  });

  it("opens the assistant a call to action asked for", () => {
    renderPage({ connect: "odoo_mcp" });
    expect(screen.getByRole("dialog")).toHaveTextContent("dialog:odoo_mcp");
  });

  it("tells a member when nothing is installed", async () => {
    integrations.mockReturnValue({ data: [], isLoading: false });
    renderPage();
    expect(
      await screen.findByText(
        "Deine App ist nicht dabei? Bitte deinen Admin, sie unter Capas zu installieren.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Capas/ })).toBeNull();
  });

  it("points whoever may install plugins to Capas, also when nothing is installed", async () => {
    may.mockImplementation((p: string) => p === "plugin:manage");
    integrations.mockReturnValue({ data: [], isLoading: false });
    renderPage();
    const link = await screen.findByRole("link", {
      name: "Weitere Integrationen findest du unter Capas →",
    });
    expect(link).toHaveAttribute("href", "/capas");
    expect(screen.getByText(/Deine App ist nicht dabei\?/)).toBeInTheDocument();
  });

  it("adds a quiet line below the list for a member", () => {
    renderPage();
    expect(
      screen.getByText(
        "Deine App ist nicht dabei? Bitte deinen Admin, sie unter Capas zu installieren.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Microsoft 365")).toBeInTheDocument();
  });

  it("says it in English too", () => {
    window.localStorage.setItem("oc8-lang", "en");
    may.mockImplementation((p: string) => p === "plugin:manage");
    renderPage();
    expect(screen.getByText(/Don't see your app\?/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Find more integrations under Capas →" }),
    ).toHaveAttribute("href", "/capas");
  });

  it("shows the line on the organisation tab as well", () => {
    may.mockImplementation((p: string) => p === "integration:manage" || p === "plugin:manage");
    renderPage({ tab: "organisation" });
    expect(
      screen.getByRole("link", { name: "Weitere Integrationen findest du unter Capas →" }),
    ).toHaveAttribute("href", "/capas");
  });

  it("never posts a pending id for an unknown provider", () => {
    const { onConsume } = renderPage({ pending: "x", provider: "evil" });
    expect(complete).not.toHaveBeenCalled();
    expect(onConsume).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent("Das Verbinden hat nicht geklappt");
  });

  it("shows the reported error and posts nothing when the return carries both", () => {
    renderPage({ pending: "p-1", provider: "microsoft", error: "access_denied" });
    expect(complete).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Du hast die Anmeldung abgebrochen");
  });

  it("follows a call to action while the page is already open, once per request", () => {
    const { onConsume, setSearch } = renderPage({});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onConsume).not.toHaveBeenCalled();
    setSearch({ connect: "odoo_mcp" });
    expect(screen.getByRole("dialog")).toHaveTextContent("dialog:odoo_mcp");
    expect(onConsume).toHaveBeenCalledTimes(1);
    setSearch({ connect: "odoo_mcp" });
    expect(onConsume).toHaveBeenCalledTimes(1);
    setSearch({ connect: "microsoft365" });
    expect(screen.getByRole("dialog")).toHaveTextContent("dialog:microsoft365");
    expect(onConsume).toHaveBeenCalledTimes(2);
  });

  it("links whoever may install plugins, with nothing installed, to Capas inside the app", () => {
    // Installing a plugin needs plugin:manage, not integration:manage.
    may.mockImplementation((p: string) => p === "integration:manage" || p === "plugin:manage");
    integrations.mockReturnValue({ data: [], isLoading: false });
    renderPage();
    const link = screen.getByRole("link", {
      name: "Weitere Integrationen findest du unter Capas →",
    });
    expect(link).toHaveAttribute("href", "/capas");
    expect(link).toHaveAttribute("data-router-link");
  });

  it("offers no edit for an organisation access without an integration", async () => {
    may.mockImplementation(ADMIN);
    org.mockReturnValue({
      data: [
        row({
          id: "o2",
          owner: "organisation",
          accountLabel: "Loose login",
          kind: "credential",
          integration: null,
          credentialType: "odoo_login",
          status: "ok",
          createdByName: "Admin Anna",
        }),
      ],
      isLoading: false,
    });
    renderPage({ tab: "organisation" });
    const orgPanel = screen.getByRole("tabpanel", { name: "Organisation" });
    // Account, who added it and when, on one line.
    expect(within(orgPanel).getByText("Loose login · Admin Anna · 8. Nov.")).toBeInTheDocument();
    expect(within(orgPanel).getByText("Aktiv")).toBeInTheDocument();
    openMenu(/Aktionen für .*Loose login/);
    expect(await screen.findByRole("menuitem", { name: "Testen" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Bearbeiten" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Entfernen" })).toBeInTheDocument();
  });

  describe("members tab", () => {
    const many = Array.from({ length: 60 }, (_, i) =>
      row({
        id: `m${i}`,
        owner: "member",
        ownerName: `Person ${i}`,
        accountLabel: `p${i}@acme.de`,
        ...(i % 2 === 1
          ? { kind: "credential", integration: "odoo_mcp", provider: null, status: "ok" }
          : {}),
        ...(i % 3 === 0 ? { status: i % 2 === 1 ? "failed" : "needs_reauth" } : {}),
      }),
    );

    beforeEach(() => {
      may.mockImplementation(ADMIN);
      members.mockReturnValue({ data: many, isLoading: false });
    });

    const bodyRows = () => within(screen.getByRole("table")).getAllByRole("row").slice(1);

    it("pages through the connections 25 at a time", () => {
      renderPage({ tab: "members" });
      expect(screen.getByRole("tab", { name: "Mitglieder (60)" })).toBeInTheDocument();
      expect(screen.getByText("1–25 von 60")).toBeInTheDocument();
      expect(bodyRows()).toHaveLength(25);
      expect(screen.getByRole("button", { name: "Vorherige Seite" })).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: "Nächste Seite" }));
      expect(screen.getByText("26–50 von 60")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Nächste Seite" }));
      expect(screen.getByText("51–60 von 60")).toBeInTheDocument();
      expect(bodyRows()).toHaveLength(10);
      expect(screen.getByRole("button", { name: "Nächste Seite" })).toBeDisabled();
    });

    it("finds a person by name or account and goes back to the first page", () => {
      renderPage({ tab: "members" });
      fireEvent.click(screen.getByRole("button", { name: "Nächste Seite" }));
      const search = screen.getByRole("searchbox", { name: "Person oder Konto suchen" });
      fireEvent.change(search, { target: { value: "person 42" } });
      expect(bodyRows()).toHaveLength(1);
      expect(screen.getByText("p42@acme.de")).toBeInTheDocument();
      expect(screen.getByText("1–1 von 1")).toBeInTheDocument();
      fireEvent.change(search, { target: { value: "P7@ACME" } });
      expect(bodyRows()).toHaveLength(1);
      expect(screen.getByText("Person 7")).toBeInTheDocument();
    });

    it("filters by app and by status", () => {
      renderPage({ tab: "members" });
      fireEvent.change(screen.getByRole("combobox", { name: "App" }), {
        target: { value: "Odoo" },
      });
      expect(screen.getByText("1–25 von 30")).toBeInTheDocument();
      fireEvent.change(screen.getByRole("combobox", { name: "Status" }), {
        target: { value: "needs_reauth" },
      });
      // Odd indices divisible by 3: 3, 9, 15, ... 57.
      expect(screen.getByText("1–10 von 10")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Nächste Seite" })).not.toBeInTheDocument();
      fireEvent.change(screen.getByRole("combobox", { name: "Status" }), {
        target: { value: "active" },
      });
      expect(screen.getByText("1–20 von 20")).toBeInTheDocument();
    });

    it("says so in one line when nothing matches", () => {
      renderPage({ tab: "members" });
      fireEvent.change(screen.getByRole("searchbox", { name: "Person oder Konto suchen" }), {
        target: { value: "nobody" },
      });
      expect(screen.getByText("Keine Verbindung passt zur Suche.")).toBeInTheDocument();
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
    });

    it("says so in one line when no member has connected anything", () => {
      members.mockReturnValue({ data: [], isLoading: false });
      renderPage({ tab: "members" });
      expect(screen.getByText("Noch kein Mitglied hat ein Konto verbunden.")).toBeInTheDocument();
      expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    });
  });
});
