import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { AgentBuilderDTO } from "@/lib/agent-builder-api";
import { builderRefusalText } from "@/lib/agent-builder-api";

const m = vi.hoisted(() => ({
  card: undefined as unknown,
  error: false,
  apply: vi.fn(),
  request: vi.fn(),
  run: vi.fn(),
  navigate: vi.fn(),
  may: true,
  applyError: null as unknown,
  applyPending: false,
}));
vi.mock("@/lib/agent-builder-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/agent-builder-api")>()),
  useAgentBuilder: () => ({ data: m.card, isLoading: false, isError: m.error }),
  useApplyAgentBuilder: () => ({
    mutate: m.apply,
    isPending: m.applyPending,
    error: m.applyError,
  }),
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
  m.error = false;
  m.applyError = null;
  m.applyPending = false;
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

  it.each([403, 404])("says the preview is unavailable when the read fails (%i)", () => {
    m.card = undefined;
    m.error = true;
    const { container } = render(<AgentBuilderCard proposalId="p1" heading="What happens" />);
    expect(screen.getByText("Preview unavailable")).toBeInTheDocument();
    expect(screen.queryByText("What happens")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(container.querySelector(".animate-pulse")).toBeNull();
  });

  it("shows its heading once the card is there", () => {
    m.card = draft;
    render(<AgentBuilderCard proposalId="p1" readOnly heading="What happens" />);
    expect(screen.getByText("What happens")).toBeInTheDocument();
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

  it("says the created agent waits for hiring approval", () => {
    m.card = {
      ...draft,
      status: "applied",
      mayCreate: false,
      mayRequest: false,
      agent: { id: "ag1", name: "Rechnungsprüfer", status: "pending_approval" },
    };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByText("Created: Rechnungsprüfer")).toBeInTheDocument();
    expect(screen.getByText("Waiting for hiring approval")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Test now" })).not.toBeInTheDocument();
  });

  it("says so when the test run could not start", () => {
    m.run.mockImplementation((_vars: unknown, opts: { onError?: (e: Error) => void }) =>
      opts.onError?.(new ApiError("boom", 500)),
    );
    m.card = {
      ...draft,
      status: "applied",
      mayCreate: false,
      mayRequest: false,
      agent: { id: "ag1", name: "Rechnungsprüfer", status: "stopped" },
    };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Test now" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Could not start the test");
    expect(m.navigate).not.toHaveBeenCalled();
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

  it("says so when the agent was already created", () => {
    m.card = draft;
    m.applyError = new ApiError("agent_builder:already_applied", 409);
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByRole("alert")).toHaveTextContent("This agent was already created.");
  });

  it("shows the generic text for any other failure", () => {
    m.card = draft;
    m.applyError = new ApiError("boom", 500);
    const { unmount } = render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByRole("alert")).toHaveTextContent("The agent was not created. Try again.");
    unmount();
    m.applyError = new TypeError("Failed to fetch");
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByRole("alert")).toHaveTextContent("The agent was not created. Try again.");
  });

  it("prefers the refusal stored on the proposal over a client error", () => {
    m.card = { ...draft, refusal: "name_taken" };
    m.applyError = new ApiError("boom", 500);
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByRole("alert")).toHaveTextContent("That name is already taken.");
  });

  it("disables the button while creating", () => {
    m.card = draft;
    m.applyPending = true;
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByRole("button", { name: "Create this agent" })).toBeDisabled();
  });

  it("labels the applied and expired states", () => {
    m.card = { ...draft, status: "applied", mayCreate: false, agent: null };
    const { unmount } = render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByRole("status")).toHaveTextContent("Created");
    unmount();
    m.card = { ...draft, status: "expired" };
    render(<AgentBuilderCard proposalId="p1" />);
    expect(screen.getByRole("status")).toHaveTextContent("Expired");
  });

  it("asks the copilot to propose again", () => {
    const propose = vi.fn();
    m.card = { ...draft, status: "expired" };
    render(<AgentBuilderCard proposalId="p1" onPropose={propose} />);
    fireEvent.click(screen.getByRole("button", { name: "Propose again" }));
    expect(propose).toHaveBeenCalledWith("/agent Please propose Rechnungsprüfer again.");
  });
});

describe("builderRefusalText", () => {
  const t = (en: string) => en;
  const codes = [
    "name_taken",
    "department_unknown",
    "connection_not_allowed",
    "model_not_allowed",
    "skill_not_allowed",
    "secret_value",
    "already_applied",
    "expired",
    "not_open",
    "superseded_concurrently",
    "step_failed",
    "something_else",
  ];
  it("has a distinct non-empty text for every code", () => {
    const texts = codes.map((c) => builderRefusalText(c, 1, t));
    for (const x of texts) expect(x).not.toBe("");
    expect(new Set(texts).size).toBe(codes.length);
  });
  it("numbers the failed step and tolerates none", () => {
    expect(builderRefusalText("step_failed", 2, t)).toContain("Step 3");
    expect(builderRefusalText("step_failed", null, t)).toContain("Step 1");
  });
  it("names a concurrent replacement", () => {
    expect(builderRefusalText("superseded_concurrently", null, t)).toBe(
      "Replaced by a newer proposal meanwhile.",
    );
  });
});
