import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: m.error, success: vi.fn(), warning: vi.fn() } }));

import { ApprovalsWidget } from "@/components/dashboard/widgets/approvals-widget";
import { ApiError } from "@/lib/api";
import * as hooks from "@/lib/hooks";

describe("ApprovalsWidget decision refusal", () => {
  it("shows a builder refusal code as its localized text", async () => {
    const approval = {
      id: "a1",
      title: "Create agent: Rechnungsprüfer",
      agentId: "copilot",
      agentName: "Copilot",
      departmentId: "dept-1",
      departmentName: "Sales",
      amount: null,
      createdAt: new Date().toISOString(),
      toolArguments: {},
      options: [],
      recommendation: null,
      actionType: "copilot.agent_builder",
      detail: null,
      taskTitle: null,
      toolName: null,
    };
    vi.spyOn(hooks, "useApprovals").mockReturnValue({
      data: [approval],
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
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({
      isPending: false,
      mutateAsync: vi.fn().mockRejectedValue(new ApiError("agent_builder:name_taken", 409)),
    } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ApprovalsWidget config={{}} onConfigChange={vi.fn()} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByText("Create agent: Rechnungsprüfer"));
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(m.error).toHaveBeenCalledWith("That name is already taken."));
  });
});
