import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentBuilderDTO } from "@/lib/agent-builder-api";

const m = vi.hoisted(() => ({
  card: undefined as unknown,
  apply: vi.fn(),
  request: vi.fn(),
  run: vi.fn(),
  navigate: vi.fn(),
  may: true,
}));
vi.mock("@/lib/agent-builder-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/agent-builder-api")>()),
  useAgentBuilder: () => ({ data: m.card, isLoading: false }),
  useApplyAgentBuilder: () => ({ mutate: m.apply, isPending: false, error: null }),
  useRequestAgentBuilder: () => ({ mutate: m.request, isPending: false, error: null }),
}));
vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useRunAgent: () => ({ mutate: m.run, isPending: false }),
}));
vi.mock("@/lib/governance-hooks", () => ({ useMay: () => () => m.may }));
vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => m.navigate,
  Link: ({ children }: { children: React.ReactNode }) => <a href="#agent">{children}</a>,
}));

import { AgentBuilderCard } from "@/components/conversation/agent-builder-card";

const draft: AgentBuilderDTO = {
  id: "p1",
  status: "draft",
  sessionId: "s1",
  departmentId: "d1",
  departmentName: "Buchhaltung",
  name: "Rechnungsprüfer",
  roleTitle: "Buchhaltung",
  mission: "Prüft jeden Morgen offene Rechnungen.",
  model: { id: "m1", label: "Mistral" },
  skills: [{ id: "s1", label: "Rechnungen prüfen" }],
  triggers: [
    {
      kind: "cron",
      cronExpression: "0 7 * * 1-5",
      eventSource: null,
      eventType: null,
      taskText: "Prüfe die offenen Rechnungen.",
    },
  ],
  systems: ["odoo"],
  limits: [{ connectionName: "odoo", function: "post_invoice", decision: "approval_required" }],
  expiresAt: "2026-11-16T09:00:00Z",
  mayCreate: true,
  mayRequest: true,
  readOnly: false,
  refusal: null,
  failedStep: null,
  rejectedReason: null,
  agent: null,
};

beforeEach(() => {
  m.apply.mockReset();
  m.request.mockReset();
  m.run.mockReset();
  m.navigate.mockReset();
  m.may = true;
});

describe("AgentBuilderCard", () => {
  it("shows what will be created and creates it", () => {
    m.card = draft;
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByText("Rechnungsprüfer")).toBeInTheDocument();
    expect(screen.getByText("Buchhaltung", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("Mistral")).toBeInTheDocument();
    expect(screen.getByText("Rechnungen prüfen")).toBeInTheDocument();
    expect(screen.getByText("odoo")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create this agent" }));
    expect(m.apply).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Send for approval" })).not.toBeInTheDocument();
  });

  it("offers approval to a member who may not create", () => {
    m.card = { ...draft, mayCreate: false };
    render(<AgentBuilderCard proposalId="p1" />);
    fireEvent.click(screen.getByRole("button", { name: "Send for approval" }));
    expect(m.request).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Create this agent" })).not.toBeInTheDocument();
  });

  it("says it is waiting while the approval is open", () => {
    m.card = { ...draft, status: "awaiting_approval", mayCreate: false, mayRequest: false };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByText("Waiting for approval")).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /create|approval/i })).toHaveLength(0);
  });

  it("links the created agent and starts a test run", () => {
    m.card = {
      ...draft,
      status: "applied",
      mayCreate: false,
      mayRequest: false,
      agent: { id: "ag1", name: "Rechnungsprüfer", status: "stopped" },
    };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByText("Created: Rechnungsprüfer")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Test now" }));
    expect(m.run).toHaveBeenCalledWith(
      { agentId: "ag1", task: "Prüfe die offenen Rechnungen." },
      expect.anything(),
    );
  });

  it("hides the test run without run:start", () => {
    m.may = false;
    m.card = {
      ...draft,
      status: "applied",
      agent: { id: "ag1", name: "Rechnungsprüfer", status: "stopped" },
    };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.queryByRole("button", { name: "Test now" })).not.toBeInTheDocument();
  });

  it("shows a rejection with its reason", () => {
    m.card = { ...draft, status: "rejected", rejectedReason: "Zu teuer" };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByText("Rejected: Zu teuer")).toBeInTheDocument();
  });

  it("collapses a superseded proposal to one line", () => {
    m.card = { ...draft, status: "superseded" };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByText("Replaced by a newer proposal")).toBeInTheDocument();
    expect(screen.queryByText("Rechnungsprüfer")).not.toBeInTheDocument();
  });

  it("offers a new proposal once expired", () => {
    const propose = vi.fn();
    m.card = { ...draft, status: "expired" };
    render(<AgentBuilderCard proposalId="p1" onPropose={propose} />);
    fireEvent.click(screen.getByRole("button", { name: "Propose again" }));
    expect(propose).toHaveBeenCalled();
  });

  it("reads only, in an approver's pane", () => {
    m.card = { ...draft, readOnly: true, mayCreate: false, mayRequest: false };
    render(<AgentBuilderCard proposalId="p1" readOnly />);
    expect(screen.queryAllByRole("button", { name: /create|approval/i })).toHaveLength(0);
  });

  it("names why the last apply was refused", () => {
    m.card = { ...draft, refusal: "name_taken", failedStep: 0 };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByRole("alert")).toHaveTextContent("That name is already taken.");
  });
});
