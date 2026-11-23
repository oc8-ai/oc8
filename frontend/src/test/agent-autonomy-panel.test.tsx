import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentDetail } from "@/lib/hooks-agent-detail";

// E2E F2: nothing could make an agent autonomous or grant it the outward
// tier, so every send of an "autonomous" guardrail preset still asked.

const mutate = vi.fn();
vi.mock("@/lib/hooks-agent-detail", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-agent-detail")>();
  return { ...actual, useSetAgentAutonomy: () => ({ mutate, isPending: false }) };
});

import { AgentAutonomyPanel } from "@/components/agent-autonomy-panel";

function agent(over: Partial<AgentDetail> = {}): AgentDetail {
  return {
    id: "a1",
    name: "Nora",
    autonomy: "default",
    autonomyGrants: [],
    ...over,
  } as AgentDetail;
}

describe("AgentAutonomyPanel", () => {
  beforeEach(() => mutate.mockReset());

  it("makes the agent autonomous and lets it reach the outside unasked", () => {
    render(<AgentAutonomyPanel agent={agent()} mayManage />);
    const outward = screen.getByRole("checkbox", { name: /outside without asking/i });
    // A grant means nothing below "autonomous".
    expect(outward).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: /autonomous/i }));
    expect(outward).toBeEnabled();
    fireEvent.click(outward);
    // What it means is spelled out before it is saved.
    expect(screen.getByText(/without a person approving each one/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    expect(mutate).toHaveBeenCalledWith(
      { autonomy: "autonomous", grants: ["outward"] },
      expect.anything(),
    );
  });

  it("drops the grant when leaving autonomous", () => {
    render(
      <AgentAutonomyPanel
        agent={agent({ autonomy: "autonomous", autonomyGrants: ["outward"] })}
        mayManage
      />,
    );
    expect(screen.getByRole("checkbox", { name: /outside without asking/i })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: /standard/i }));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(mutate).toHaveBeenCalledWith({ autonomy: "default", grants: [] }, expect.anything());
  });

  it("keeps the agent's other grants when only the outward one changes", () => {
    // Review of F2: the panel shows the outward tier only; a grant set some
    // other way (destructive) must survive a save here.
    render(
      <AgentAutonomyPanel
        agent={agent({ autonomy: "autonomous", autonomyGrants: ["destructive", "outward"] })}
        mayManage
      />,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: /outside without asking/i }));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(mutate).toHaveBeenCalledWith(
      { autonomy: "autonomous", grants: ["destructive"] },
      expect.anything(),
    );
  });

  it("is read-only without the right to manage the agent", () => {
    render(<AgentAutonomyPanel agent={agent()} mayManage={false} />);
    expect(screen.getByRole("radio", { name: /autonomous/i })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
  });

  it("carries the anchor the guardrail hint links to", () => {
    const { container } = render(<AgentAutonomyPanel agent={agent()} mayManage />);
    expect(container.querySelector("#agent-autonomy")).not.toBeNull();
  });
});
