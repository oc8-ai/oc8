// The inbox dialog's decision bar sits below the scrolling content, never
// over it. jsdom does no layout, so this pins the structure that makes it so:
// a height-bounded column dialog, a scroll body that may shrink (min-h-0)
// with bottom padding, and the bar as its own non-sticky row after it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/conversation/agent-builder-card", () => ({
  AgentBuilderCard: () => <div data-testid="builder-preview">Auftrag</div>,
}));
vi.mock("@/components/conversation/discuss-with-copilot", () => ({
  DiscussWithCopilot: () => null,
}));

import { ApprovalsWidget } from "@/components/dashboard/widgets/approvals-widget";
import * as hooks from "@/lib/hooks";

function classes(el: Element | null): string[] {
  return (el?.getAttribute("class") ?? "").split(/\s+/);
}

describe("the approvals inbox dialog", () => {
  it("keeps the decision bar below the scrolling content", () => {
    vi.spyOn(hooks, "useApprovals").mockReturnValue({
      data: [
        {
          id: "a1",
          title: "Create agent: Rechnungsprüfer",
          agentId: "copilot",
          agentName: "Copilot",
          departmentId: "dept-1",
          departmentName: "Buchhaltung",
          amount: null,
          createdAt: new Date().toISOString(),
          toolArguments: {},
          options: [],
          recommendation: null,
          actionType: "copilot.agent_builder",
          proposalId: "p1",
          detail: null,
          taskTitle: null,
          toolName: null,
        },
      ],
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useClarifications").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      seats: [{ departmentId: "dept-1", seatRole: "dept_approver" }],
      decidesEverywhere: true,
      unrestricted: true,
      unassigned: false,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ApprovalsWidget config={{}} onConfigChange={vi.fn()} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByText("Create agent: Rechnungsprüfer"));

    const dialog = screen.getByRole("dialog");
    expect(classes(dialog)).toEqual(
      expect.arrayContaining(["flex", "flex-col", "overflow-hidden", "max-h-[85dvh]"]),
    );
    expect(classes(dialog)).not.toContain("grid");

    const footer = screen.getByRole("button", { name: "Approve" }).closest("footer");
    expect(footer).not.toBeNull();
    expect(classes(footer)).toContain("shrink-0");
    expect(classes(footer)).not.toContain("sticky");

    const body = footer!.previousElementSibling;
    expect(classes(body)).toEqual(
      expect.arrayContaining(["min-h-0", "flex-1", "overflow-y-auto", "pb-8"]),
    );
    expect(body).toContainElement(screen.getByTestId("builder-preview"));
    expect(footer).not.toContainElement(screen.getByTestId("builder-preview"));

    const pane = footer!.parentElement;
    expect(classes(pane)).toEqual(
      expect.arrayContaining(["flex", "min-h-0", "flex-1", "flex-col"]),
    );
    expect(dialog).toContainElement(pane);
  });
});
