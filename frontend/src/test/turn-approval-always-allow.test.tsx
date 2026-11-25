import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Approval } from "@/lib/hooks";

const m = vi.hoisted(() => ({ decide: vi.fn() }));
vi.mock("@/lib/direct-access-api", () => ({
  useDirectAccessGrants: () => ({ data: [] }),
  useDecideDirectAccess: () => ({ mutate: m.decide, isPending: false }),
  useRevokeDirectAccess: () => ({ mutate: vi.fn(), isPending: false }),
  useMyDirectAccessApprovals: () => ({ data: [] }),
}));

// The turn's held-write card: TurnApprovalCards renders TURN_APPROVAL_CARDS,
// and a member-owned `tool_send` card is the direct_access entry's Card.
import { TURN_APPROVAL_CARDS } from "@/components/conversation/turn-approval-registry";

const Card = TURN_APPROVAL_CARDS.direct_access.Card;

const write: Approval = {
  id: "ap2",
  agentId: "copilot",
  title: "Copilot on behalf of Anna: change Odoo (update_record)",
  detail: "",
  amount: null,
  time: "",
  status: "pending",
  actionType: "tool_send",
  runId: "r1",
  alwaysAllowOffered: true,
  directAccess: {
    sessionId: "s1",
    toolKey: "odoo",
    label: "Odoo",
    memberName: "Anna",
    account: "own",
    decider: "member",
    grantId: null,
    grantActive: false,
  },
};

const ALWAYS = /always allow for me/i;

describe("the write card's always allow", () => {
  beforeEach(() => m.decide.mockReset());

  it('the write card offers "Always allow for me" only for write-tier member cards', () => {
    const { unmount } = render(<Card approval={write} />);
    expect(screen.getByRole("checkbox", { name: ALWAYS })).toBeInTheDocument();
    expect(screen.getByText(/never deleting or sending outside/i)).toBeInTheDocument();
    unmount();

    // Not write tier (the server does not offer it), a department's card, an
    // offer, or a card already decided: no checkbox.
    for (const approval of [
      { ...write, alwaysAllowOffered: false },
      { ...write, directAccess: { ...write.directAccess!, decider: "department" as const } },
      { ...write, actionType: "direct_access" },
      { ...write, status: "approved" },
    ]) {
      const view = render(<Card approval={approval} />);
      expect(screen.queryByRole("checkbox", { name: ALWAYS })).toBeNull();
      view.unmount();
    }
  });

  it("sends alwaysAllow with the approval only when ticked", () => {
    const { unmount } = render(<Card approval={write} />);
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(m.decide).toHaveBeenLastCalledWith(
      { approvalId: "ap2", decision: "approve" },
      expect.anything(),
    );
    unmount();

    render(<Card approval={write} />);
    fireEvent.click(screen.getByRole("checkbox", { name: ALWAYS }));
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(m.decide).toHaveBeenLastCalledWith(
      { approvalId: "ap2", decision: "approve", alwaysAllow: true },
      expect.anything(),
    );
    // A rejection never carries it.
    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(m.decide).toHaveBeenLastCalledWith(
      { approvalId: "ap2", decision: "reject" },
      expect.anything(),
    );
  });
});
