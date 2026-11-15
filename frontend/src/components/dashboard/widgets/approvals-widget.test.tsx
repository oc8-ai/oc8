// frontend/src/components/dashboard/widgets/approvals-widget.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApprovalsWidget } from "./approvals-widget";
import * as hooks from "@/lib/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("ApprovalsWidget", () => {
  it("shows the empty state when nothing is waiting", () => {
    vi.spyOn(hooks, "useApprovals").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useClarifications").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      seats: [],
      decidesEverywhere: true,
      unrestricted: false,
      unassigned: false,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);

    render(<ApprovalsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByText(/nothing is waiting for you/i)).toBeInTheDocument();
  });

  it("opens the detail dialog for a row on click", async () => {
    const approval = {
      id: "a1",
      title: "Refund a customer",
      agentId: "agent-1",
      agentName: "Agent One",
      departmentId: "dept-1",
      departmentName: "Sales",
      amount: "€50",
      createdAt: new Date().toISOString(),
      toolArguments: {},
      options: [],
      recommendation: null,
      actionType: "tool_send",
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
      decidesEverywhere: false,
      unrestricted: false,
      unassigned: false,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);

    render(<ApprovalsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByText("Refund a customer"));
    await waitFor(() => expect(screen.getByText("Approve")).toBeInTheDocument());
  });

  it("filters the queue by kind and by department", () => {
    const approval = {
      id: "a1",
      title: "Refund a customer",
      agentId: "agent-1",
      agentName: "Agent One",
      departmentId: "dept-1",
      departmentName: "Sales",
      amount: "€50",
      createdAt: new Date().toISOString(),
      toolArguments: {},
      options: [],
      recommendation: null,
      actionType: "tool_send",
      detail: null,
      taskTitle: null,
      toolName: null,
    };
    const clarification = {
      id: "c1",
      runId: "run-1",
      agentId: "agent-2",
      agentName: "Agent Two",
      departmentId: "dept-2",
      departmentName: "Support",
      question: "Which invoice?",
      status: "pending",
      createdAt: new Date().toISOString(),
    };
    vi.spyOn(hooks, "useApprovals").mockReturnValue({
      data: [approval],
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useClarifications").mockReturnValue({
      data: [clarification],
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      seats: [],
      decidesEverywhere: true,
      unrestricted: false,
      unassigned: false,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);

    render(<ApprovalsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });

    expect(screen.getByText("Refund a customer")).toBeInTheDocument();
    expect(screen.getByText("Which invoice?")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Questions"));
    expect(screen.queryByText("Refund a customer")).not.toBeInTheDocument();
    expect(screen.getByText("Which invoice?")).toBeInTheDocument();

    fireEvent.click(screen.getByText("All"));
    fireEvent.click(screen.getByRole("button", { name: "Sales" }));
    expect(screen.getByText("Refund a customer")).toBeInTheDocument();
    expect(screen.queryByText("Which invoice?")).not.toBeInTheDocument();
  });

  it("lists an answered clarification under Decided by you", async () => {
    const clarification = {
      id: "c1",
      runId: "run-1",
      agentId: "agent-2",
      agentName: "Agent Two",
      departmentId: "dept-2",
      departmentName: "Support",
      question: "Which invoice?",
      status: "pending",
      createdAt: new Date().toISOString(),
    };
    vi.spyOn(hooks, "useApprovals").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useClarifications").mockReturnValue({
      data: [clarification],
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      seats: [{ departmentId: "dept-2", seatRole: "dept_approver" }],
      decidesEverywhere: false,
      unrestricted: false,
      unassigned: false,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    const answerMutateAsync = vi.fn().mockResolvedValue({});
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({
      isPending: false,
      mutateAsync: answerMutateAsync,
    } as never);

    render(<ApprovalsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByText("Which invoice?"));
    const textarea = await screen.findByLabelText(/your answer/i);
    fireEvent.change(textarea, { target: { value: "Invoice #42" } });
    fireEvent.click(screen.getByRole("button", { name: /answer/i }));

    await waitFor(() => expect(answerMutateAsync).toHaveBeenCalled());
    expect(await screen.findByText("Decided by you")).toBeInTheDocument();
    expect(screen.getByText("Which invoice?")).toBeInTheDocument();
  });

  it("shows the unassigned state instead of an empty queue for a member with no seats", () => {
    vi.spyOn(hooks, "useApprovals").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useClarifications").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      seats: [],
      decidesEverywhere: false,
      unrestricted: false,
      unassigned: true,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);

    render(<ApprovalsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByText(/not assigned to a department/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing is waiting for you/i)).not.toBeInTheDocument();
  });

  it("renders a structured reason sentence from reasonContext instead of the raw detail string", async () => {
    const approval = {
      id: "a1",
      title: "Vera wants to send a quote",
      agentId: "agent-1",
      agentName: "Vera",
      departmentId: "dept-1",
      departmentName: "Sales",
      amount: "€7,400",
      createdAt: new Date().toISOString(),
      toolArguments: {},
      options: [],
      recommendation: null,
      actionType: "tool_send",
      detail: "condition 'order_value >= 5000' matched",
      taskTitle: null,
      toolName: null,
      reasonContext: {
        code: "condition_matched",
        attribute: "order_value",
        operator: ">=",
        threshold: 5000,
        actual: 7400,
      },
    };
    vi.spyOn(hooks, "useApprovals").mockReturnValue({
      data: [approval],
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useClarifications").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      seats: [{ departmentId: "dept-1", seatRole: "dept_approver" }],
      decidesEverywhere: false,
      unrestricted: false,
      unassigned: false,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);

    render(<ApprovalsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByText("Vera wants to send a quote"));
    expect(await screen.findByText(/Order Value ≥/)).toBeInTheDocument();
    expect(screen.getByText(/this one is/)).toBeInTheDocument();
    expect(screen.queryByText("condition 'order_value >= 5000' matched")).not.toBeInTheDocument();
  });

  it("shows the underlying error message when the queue fails to load", () => {
    vi.spyOn(hooks, "useApprovals").mockReturnValue({
      data: undefined,
      error: new Error("network unreachable"),
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useClarifications").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      seats: [],
      decidesEverywhere: true,
      unrestricted: false,
      unassigned: false,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);

    render(<ApprovalsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByText("network unreachable")).toBeInTheDocument();
  });

  it("draws its own title as the first line", () => {
    vi.spyOn(hooks, "useApprovals").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useClarifications").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useStanding").mockReturnValue({
      seats: [],
      decidesEverywhere: true,
      unrestricted: false,
      unassigned: false,
      pending: false,
    } as never);
    vi.spyOn(hooks, "useDecideApproval").mockReturnValue({ isPending: false } as never);
    vi.spyOn(hooks, "useAnswerClarification").mockReturnValue({ isPending: false } as never);
    render(<ApprovalsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    // The filter pill row also says "Approvals"; the title is the non-button one.
    const titles = screen.getAllByText("Approvals").filter((el) => !el.closest("button"));
    expect(titles).toHaveLength(1);
  });
});
