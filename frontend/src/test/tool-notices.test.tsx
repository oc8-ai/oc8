import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ToolNotices } from "@/components/connections/tool-notices";
import { LanguageProvider } from "@/lib/i18n";

function renderNotices(value: Parameters<typeof ToolNotices>[0]["value"]) {
  return render(
    <LanguageProvider>
      <ToolNotices value={value} />
    </LanguageProvider>,
  );
}

describe("ToolNotices", () => {
  afterEach(() => window.localStorage.removeItem("oc8-lang"));

  it("names an incomplete login as a quiet info line, not an alert", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotices([
      {
        tool: "erp",
        integration: "erp_pack",
        label: "ERP",
        reason: "incomplete",
        missing: ["url"],
      },
    ]);
    const line = screen.getByRole("status");
    expect(line).toHaveTextContent(
      "ERP war in diesem Lauf nicht verfügbar: Der Zugang ist unvollständig (url fehlt). Bitte eine Admin-Person, ihn unter Einstellungen → Verbindungen zu vervollständigen.",
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("names several enabled logins without a choice", () => {
    renderNotices([
      { tool: "erp", integration: "erp_pack", label: "ERP", reason: "ambiguous", missing: [] },
    ]);
    expect(screen.getByRole("status")).toHaveTextContent(
      "ERP was not available in this run: several logins are enabled for this department and the agent has none chosen.",
    );
  });

  it("renders nothing without notices", () => {
    const { container } = renderNotices([]);
    expect(container).toBeEmptyDOMElement();
  });
});
