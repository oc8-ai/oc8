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
  return { ...actual, useCapaIcon: (id: string) => capaIcon(id) };
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
vi.mock("@/components/connections/connect-dialog", () => ({
  ConnectDialog: ({ integration, open }: { integration: string; open: boolean }) =>
    open ? <div role="dialog">dialog:{integration}</div> : null,
}));

import { ConnectionsPage, type ConnectionsSearch } from "@/components/connections/connections-page";
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
  const tree = (next: ConnectionsSearch) => {
    const page = <ConnectionsPage search={next} onConsumeSearch={onConsume} />;
    // StrictMode wraps the page only: LanguageProvider's own mount effects
    // would otherwise reset the stored language on their second run.
    return (
      <QueryClientProvider client={qc}>
        <LanguageProvider>{strict ? <StrictMode>{page}</StrictMode> : page}</LanguageProvider>
      </QueryClientProvider>
    );
  };
  const view = render(tree(search));
  const setSearch = (next: ConnectionsSearch) => view.rerender(tree(next));
  return { onConsume, setSearch, ...view };
}

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
  });

  it("shows a member only their own connections", async () => {
    renderPage();
    const mineSection = await screen.findByRole("region", { name: "Meine Verbindungen" });
    expect(within(mineSection).getByText(/Verbunden als anna@acme.de/)).toBeInTheDocument();
    expect(within(mineSection).getByRole("button", { name: "Odoo verbinden" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Organisation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Mitglieder" })).not.toBeInTheDocument();
    expect(screen.queryByText("Ben")).not.toBeInTheDocument();
    // The admin-only lists are never even fetched for a member.
    expect(orgEnabled).toHaveBeenCalledWith(false);
    expect(orgEnabled).not.toHaveBeenCalledWith(true);
    expect(membersEnabled).not.toHaveBeenCalledWith(true);
  });

  it("shows admins the organisation and the members' connections", async () => {
    may.mockImplementation((p: string) => p === "integration:manage");
    renderPage();
    const orgSection = await screen.findByRole("region", { name: "Organisation" });
    expect(within(orgSection).getByText("Odoo system")).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Mitglieder" })).getByText("Ben"),
    ).toBeInTheDocument();
    expect(screen.getByText("further-credentials")).toBeInTheDocument();
    expect(orgEnabled).toHaveBeenCalledWith(true);
    expect(membersEnabled).toHaveBeenCalledWith(true);
  });

  it("asks before revoking a member's connection", async () => {
    may.mockImplementation((p: string) => p === "integration:manage");
    disconnect.mockResolvedValue(undefined);
    renderPage();
    const membersSection = screen.getByRole("region", { name: "Mitglieder" });
    fireEvent.click(within(membersSection).getByRole("button", { name: "Widerrufen" }));
    const confirmBox = await screen.findByRole("alertdialog");
    expect(confirmBox).toHaveTextContent("Ben verliert diese Verbindung (ben@acme.de)");
    expect(disconnect).not.toHaveBeenCalled();
    fireEvent.click(within(confirmBox).getByRole("button", { name: "Widerrufen" }));
    await waitFor(() =>
      expect(disconnect).toHaveBeenCalledWith(expect.objectContaining({ id: "b1" })),
    );
  });

  it("shows the logo on a card when the integration has one, the name alone otherwise", () => {
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
      "Ihr IT-Administrator muss diese Verbindung zuerst freigeben",
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
    expect(screen.getByRole("alert")).toHaveTextContent("Sie haben die Anmeldung abgebrochen");
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
      await screen.findByText("Ihr Admin hat noch keine Integration eingerichtet."),
    ).toBeInTheDocument();
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
    expect(screen.getByRole("alert")).toHaveTextContent("Sie haben die Anmeldung abgebrochen");
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

  it("links an admin with nothing installed to the Capas page inside the app", () => {
    may.mockImplementation((p: string) => p === "integration:manage");
    integrations.mockReturnValue({ data: [], isLoading: false });
    renderPage();
    const link = screen.getByRole("link", { name: "Integration installieren" });
    expect(link).toHaveAttribute("href", "/capas");
    expect(link).toHaveAttribute("data-router-link");
  });

  it("offers no edit for an organisation access without an integration", () => {
    may.mockImplementation((p: string) => p === "integration:manage");
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
    renderPage();
    const orgSection = screen.getByRole("region", { name: "Organisation" });
    expect(within(orgSection).getByText("Loose login")).toBeInTheDocument();
    expect(
      within(orgSection).queryByRole("button", { name: "Bearbeiten" }),
    ).not.toBeInTheDocument();
    expect(within(orgSection).getByRole("button", { name: "Testen" })).toBeInTheDocument();
    // Who added it and when are separate columns.
    expect(within(orgSection).getByRole("cell", { name: "Admin Anna" })).toBeInTheDocument();
    expect(within(orgSection).getByRole("cell", { name: /8\. Nov/ })).toBeInTheDocument();
  });
});
