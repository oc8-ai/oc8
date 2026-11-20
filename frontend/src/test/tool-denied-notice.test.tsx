import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
      return <a href={`${path}${query}`}>{children}</a>;
    },
  };
});

const permissions = vi.hoisted(() => ({ department: true, agent: true }));
vi.mock("@/lib/governance-hooks", () => ({
  useMay: () => (permission: string) =>
    permission === "department:manage" ? permissions.department : false,
  useMayManageAgent: () => () => permissions.agent,
}));

import { ToolDeniedNotice } from "@/components/connections/tool-denied-notice";
import type { ToolDeniedInfo } from "@/lib/connections";
import { LanguageProvider } from "@/lib/i18n";

const DENIED: ToolDeniedInfo = {
  connection: "microsoft365",
  tool: "mail_search",
  label: "Microsoft 365",
  right: "read",
  reason: "tool_not_in_frame",
  scope: "department",
  departmentId: "d1",
  agentId: "a1",
};

function renderNotice(value: ToolDeniedInfo | null) {
  return render(
    <LanguageProvider>
      <ToolDeniedNotice value={value} />
    </LanguageProvider>,
  );
}

describe("ToolDeniedNotice", () => {
  beforeEach(() => {
    permissions.department = true;
    permissions.agent = true;
  });
  afterEach(() => window.localStorage.removeItem("oc8-lang"));

  it("names the tool and where an admin allows it, with a link to the guardrails", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice(DENIED);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Diese Abteilung erlaubt das Werkzeug „mail_search“ (Microsoft 365) nicht. Ein Admin kann es unter Abteilung → Guardrails freigeben.",
    );
    expect(screen.getByRole("link", { name: "Guardrails öffnen →" })).toHaveAttribute(
      "href",
      "/departments/d1?tab=guardrails",
    );
  });

  it("speaks English", () => {
    window.localStorage.setItem("oc8-lang", "en");
    renderNotice(DENIED);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "This department does not allow the tool “mail_search” (Microsoft 365). An admin can enable it under Department → Guardrails.",
    );
    expect(screen.getByRole("link", { name: "Open guardrails →" })).toBeInTheDocument();
  });

  it("shows no link to someone who may not manage departments", () => {
    window.localStorage.setItem("oc8-lang", "de");
    permissions.department = false;
    renderNotice(DENIED);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("falls back to the connection key without a label", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice({ ...DENIED, label: null });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Diese Abteilung erlaubt das Werkzeug „mail_search“ (microsoft365) nicht.",
    );
  });

  it("names the connection when no single tool was asked for", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice({ ...DENIED, tool: null, reason: "connection_not_in_frame" });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Diese Abteilung erlaubt die Verbindung Microsoft 365 nicht.",
    );
  });

  it("points at the agent's guardrails when only the agent withholds it", () => {
    window.localStorage.setItem("oc8-lang", "de");
    renderNotice({ ...DENIED, scope: "agent", reason: "right_not_granted" });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Die Guardrails dieses Agenten erlauben das Werkzeug „mail_search“ (Microsoft 365) nicht. Wer den Agenten verwalten darf, kann es unter Agent → Guardrails freigeben.",
    );
    expect(screen.getByRole("link", { name: "Guardrails öffnen →" })).toHaveAttribute(
      "href",
      "/agents/a1?tab=guardrails",
    );
  });

  it("renders nothing without a denial", () => {
    const { container } = renderNotice(null);
    expect(container).toBeEmptyDOMElement();
  });
});
