import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

// The notice links through the router (no full page reload). Stub its Link
// as an anchor that renders the href it would navigate to.
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({
      children,
      to,
      params,
      search,
    }: {
      children?: ReactNode;
      to: string;
      params?: Record<string, string>;
      search?: Record<string, string>;
    }) => {
      const path = to.replace(/\$(\w+)/g, (_, key: string) => params?.[key] ?? "");
      const query = search ? `?${new URLSearchParams(search).toString()}` : "";
      return (
        <a href={`${path}${query}`} data-router-link="">
          {children}
        </a>
      );
    },
  };
});
import { ConnectionRequiredNotice } from "@/components/connections/connection-required-notice";
import { LanguageProvider } from "@/lib/i18n";

function renderNotice(
  value: Parameters<typeof ConnectionRequiredNotice>[0]["value"],
  agentId?: string,
) {
  return render(
    <LanguageProvider>
      <ConnectionRequiredNotice value={value} agentId={agentId} />
    </LanguageProvider>,
  );
}

describe("ConnectionRequiredNotice", () => {
  afterEach(() => window.localStorage.removeItem("oc8-lang"));

  it("asks for the person's Microsoft account with a link", async () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice({
      integration: "microsoft365",
      provider: "microsoft",
      label: "Microsoft 365",
      reason: "not_connected",
    });
    expect(
      await screen.findByText(
        "Für diese Aufgabe braucht der Agent Zugriff auf dein Microsoft-Konto.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Jetzt verbinden →" })).toHaveAttribute(
      "href",
      "/connections?connect=microsoft365",
    );
    expect(screen.getByRole("link")).toHaveAttribute("data-router-link");
  });

  it("explains a schedule without owner, without a connect link", () => {
    renderNotice({
      integration: "odoo_mcp",
      provider: null,
      label: "Odoo",
      reason: "schedule_without_owner",
    });
    expect(
      screen.getByText("This schedule has no owner. Please save it once again."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("says the schedule has no owner in German", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice({
      integration: "odoo_mcp",
      provider: null,
      label: "Odoo",
      reason: "schedule_without_owner",
    });
    expect(
      screen.getByText("Dieser Zeitplan hat keinen Besitzer. Bitte speicher ihn einmal neu."),
    ).toBeInTheDocument();
  });

  it("asks to reconnect an expired connection", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice({
      integration: "google_workspace",
      provider: "google",
      label: "Google Workspace",
      reason: "needs_reauth",
    });
    expect(
      screen.getByText("Die Verbindung zu deinem Google-Konto ist abgelaufen."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Jetzt erneut verbinden →" })).toHaveAttribute(
      "href",
      "/connections?connect=google_workspace",
    );
  });

  it("names a login-based integration by its label", () => {
    renderNotice({
      integration: "odoo_mcp",
      provider: null,
      label: "Odoo",
      reason: "not_connected",
    });
    expect(
      screen.getByText("For this task the agent needs your access to Odoo."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect now →" })).toHaveAttribute(
      "href",
      "/connections?connect=odoo_mcp",
    );
  });

  it("offers to set up an access that is missing", () => {
    renderNotice({
      integration: "odoo_mcp",
      provider: null,
      label: "Odoo",
      reason: "not_configured",
    });
    expect(screen.getByText("No access to Odoo is set up yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set up now →" })).toHaveAttribute(
      "href",
      "/connections?connect=odoo_mcp",
    );
  });

  it("explains a run nobody works with, without a link", () => {
    renderNotice({
      integration: "microsoft365",
      provider: "microsoft",
      label: "Microsoft 365",
      reason: "no_acting_person",
    });
    expect(
      screen.getByText(
        "This run has no acting person, so the agent cannot pick an account. Give the agent a fixed access.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("links a binding that needs confirmation to the agent's access setting", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice(
      {
        integration: "microsoft365",
        provider: "microsoft",
        label: "Microsoft 365",
        reason: "binding_needs_confirmation",
      },
      "a1",
    );
    expect(
      screen.getByText("Der Zugang dieses Agenten zu Microsoft 365 muss erneut bestätigt werden."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Zugang prüfen →" })).toHaveAttribute(
      "href",
      "/agents/a1?tab=guardrails",
    );
  });

  it("explains why a run acting for nobody may not use a personal fixed access", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice(
      {
        integration: "microsoft365",
        provider: "microsoft",
        label: "Microsoft 365",
        reason: "personal_account_withheld",
      },
      "a1",
    );
    expect(
      screen.getByText(
        "Dieser Lauf handelt für niemanden und darf daher das persönliche Konto aus dem festen Zugang dieses Agenten zu Microsoft 365 nicht nutzen.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Zugang prüfen →" })).toHaveAttribute(
      "href",
      "/agents/a1?tab=guardrails",
    );
  });

  it("renders nothing without a problem", () => {
    const { container } = renderNotice(null);
    expect(container).toBeEmptyDOMElement();
  });
  it("asks the creator to save a schedule again after the agent changed", async () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice({
      integration: "microsoft365",
      provider: "microsoft",
      label: "Microsoft 365",
      reason: "schedule_needs_confirmation",
    });
    expect(
      await screen.findByText(
        "Der Agent wurde geändert. Bitte öffne und speicher den Zeitplan erneut, damit er wieder in deinem Namen arbeitet.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("does not ask to connect when a trigger may not act for its creator", async () => {
    window.localStorage.setItem("oc8-lang", "en");
    renderNotice({
      integration: "microsoft365",
      provider: "microsoft",
      label: "Microsoft 365",
      reason: "trigger_without_person",
    });
    expect(
      await screen.findByText(
        "This trigger may use its creator's account only if the creator allows it in the trigger's settings.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
