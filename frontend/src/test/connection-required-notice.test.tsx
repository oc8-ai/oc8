import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
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
        "Für diese Aufgabe braucht der Agent Zugriff auf Ihr Microsoft-Konto.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Jetzt verbinden →" })).toHaveAttribute(
      "href",
      "/connections?connect=microsoft365",
    );
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
      screen.getByText("Zeitplan hat keinen Besitzer, bitte einmal neu speichern."),
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
      screen.getByText("Die Verbindung zu Ihrem Google-Konto ist abgelaufen."),
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
        "Nobody is working with this run, so the agent cannot choose an account. Give the agent a fixed access.",
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

  it("renders nothing without a problem", () => {
    const { container } = renderNotice(null);
    expect(container).toBeEmptyDOMElement();
  });
});
