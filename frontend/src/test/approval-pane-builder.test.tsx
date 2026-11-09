import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/conversation/agent-builder-card", () => ({
  AgentBuilderCard: ({ proposalId, readOnly }: { proposalId: string; readOnly?: boolean }) => (
    <div
      data-testid="builder-preview"
      data-proposal={proposalId}
      data-readonly={String(!!readOnly)}
    />
  ),
}));
vi.mock("@/components/conversation/discuss-with-copilot", () => ({
  DiscussWithCopilot: () => null,
}));

import { ApprovalPane } from "@/components/dashboard/widgets/approvals-widget";
import type { Approval } from "@/lib/hooks";

const approval: Approval = {
  id: "ap1",
  agentId: "copilot",
  agentName: "Copilot",
  title: "Create agent: Rechnungsprüfer",
  detail: "Buchhaltung",
  amount: null,
  time: "",
  status: "pending",
  actionType: "copilot.agent_builder",
  departmentId: "d1",
  departmentName: "Buchhaltung",
  proposalId: "p1",
};

describe("ApprovalPane for an agent builder request", () => {
  it("shows the same preview, read-only", () => {
    render(
      <ApprovalPane
        approval={approval}
        mayAct
        viewOnlyBecause="seat"
        busy={false}
        onDecide={vi.fn()}
      />,
    );
    const preview = screen.getByTestId("builder-preview");
    expect(preview).toHaveAttribute("data-proposal", "p1");
    expect(preview).toHaveAttribute("data-readonly", "true");
  });

  it("shows nothing extra for other approvals", () => {
    render(
      <ApprovalPane
        approval={{ ...approval, actionType: "tool_send", proposalId: null }}
        mayAct
        viewOnlyBecause="seat"
        busy={false}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("builder-preview")).not.toBeInTheDocument();
  });
});
