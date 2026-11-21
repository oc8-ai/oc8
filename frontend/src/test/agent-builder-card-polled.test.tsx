// The builder card names a proposed event trigger's settings, and once the
// agent is created without its polled trigger (it had no login to listen on
// yet), says what the person does next.
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AgentBuilderDTO } from "@/lib/agent-builder-api";

const m = vi.hoisted(() => ({ card: undefined as unknown }));
vi.mock("@/lib/agent-builder-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/agent-builder-api")>()),
  useAgentBuilder: () => ({ data: m.card, isLoading: false, isError: false }),
  useApplyAgentBuilder: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRequestAgentBuilder: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));
vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useRunAgent: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/lib/governance-hooks", () => ({ useMay: () => () => true }));
vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => vi.fn(),
  Link: ({ children }: { children: React.ReactNode }) => <a href="#agent">{children}</a>,
}));

import { AgentBuilderCard } from "@/components/conversation/agent-builder-card";

const card: AgentBuilderDTO = {
  id: "p1",
  status: "draft",
  sessionId: "s1",
  departmentId: "d1",
  departmentName: "Einkauf",
  newDepartment: false,
  name: "Postsortierer",
  roleTitle: "",
  mission: "Sortiert die Post.",
  model: null,
  skills: [],
  triggers: [
    {
      kind: "event",
      cronExpression: null,
      eventSource: "email",
      eventType: "mail.received",
      taskText: "Sortier sie.",
      settings: [
        { key: "folder", label: "Folder", labelTranslations: { de: "Ordner" }, value: "Orders" },
      ],
    },
  ],
  systems: [],
  limits: [],
  expiresAt: null,
  mayCreate: true,
  mayRequest: true,
  readOnly: false,
  refusal: null,
  failedStep: null,
  failedReason: null,
  rejectedReason: null,
  agent: null,
  kind: "agent",
  board: null,
};

describe("builder card and polled triggers", () => {
  it("shows the trigger's settings with their labels", () => {
    m.card = card;
    render(<AgentBuilderCard proposalId="p1" />);

    expect(screen.getByText("Folder: Orders")).toBeInTheDocument();
  });

  it("says the agent was created without its email trigger", () => {
    m.card = {
      ...card,
      status: "applied",
      agent: { id: "a1", name: "Postsortierer", status: "stopped" },
      notices: ["trigger_needs_login"],
    };
    render(<AgentBuilderCard proposalId="p1" />);

    expect(
      screen.getByText(
        "The agent was created without its email trigger: pick its mailbox in the agent's tool settings, then add the trigger on the agent's page.",
      ),
    ).toHaveAttribute("role", "status");
  });
});
