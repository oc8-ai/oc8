import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { TaskBoardWidget } from "./task-board-widget";
import * as hooks from "@/lib/hooks";
import { ApiError } from "@/lib/api";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const departments = [
  { id: "dept-1", name: "Kundenservice" },
  { id: "dept-2", name: "Vertrieb" },
];

describe("TaskBoardWidget", () => {
  it("shows the empty state when the board is empty", () => {
    vi.spyOn(hooks, "useTaskBoard").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useDepartments").mockReturnValue({
      data: { items: departments, totalCount: 2 },
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useCreateTask").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never);

    render(<TaskBoardWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByText(/no tasks yet/i)).toBeInTheDocument();
  });

  it("groups tasks into their board column", () => {
    vi.spyOn(hooks, "useTaskBoard").mockReturnValue({
      data: [
        {
          id: "t1",
          title: "Tickets zusammenfassen",
          state: "in_progress",
          column: "in_progress",
          departmentId: "dept-1",
          departmentName: "Kundenservice",
          agentId: "agent-1",
          agentName: "Nora",
          requestedByMemberId: null,
          parentTaskId: null,
          delegationDepth: 0,
          createdAt: new Date().toISOString(),
        },
        {
          id: "t2",
          title: "Angebot nachfassen",
          state: "backlog",
          column: "backlog",
          departmentId: "dept-2",
          departmentName: "Vertrieb",
          agentId: null,
          agentName: null,
          requestedByMemberId: null,
          parentTaskId: null,
          delegationDepth: 0,
          createdAt: new Date().toISOString(),
        },
      ],
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useDepartments").mockReturnValue({
      data: { items: departments, totalCount: 2 },
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useCreateTask").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never);

    render(<TaskBoardWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByText("Tickets zusammenfassen")).toBeInTheDocument();
    expect(screen.getByText("Angebot nachfassen")).toBeInTheDocument();
  });

  it("creates a task from the dialog", async () => {
    vi.spyOn(hooks, "useTaskBoard").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useDepartments").mockReturnValue({
      data: { items: departments, totalCount: 2 },
      isPending: false,
    } as never);
    const mutateAsync = vi.fn().mockResolvedValue({});
    vi.spyOn(hooks, "useCreateTask").mockReturnValue({ mutateAsync, isPending: false } as never);

    render(<TaskBoardWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));

    const instructions = await screen.findByLabelText(/instructions/i);
    fireEvent.change(instructions, { target: { value: "Bitte Tickets zusammenfassen." } });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        departmentId: "dept-1",
        instructions: "Bitte Tickets zusammenfassen.",
        title: undefined,
      }),
    );
  });

  it("surfaces a leaderless department as a readable error, not a raw 409", async () => {
    vi.spyOn(hooks, "useTaskBoard").mockReturnValue({ data: [], isPending: false } as never);
    vi.spyOn(hooks, "useDepartments").mockReturnValue({
      data: { items: departments, totalCount: 2 },
      isPending: false,
    } as never);
    const mutateAsync = vi.fn().mockRejectedValue(new ApiError("no team lead", 409));
    vi.spyOn(hooks, "useCreateTask").mockReturnValue({ mutateAsync, isPending: false } as never);
    const { toast } = await import("sonner");

    render(<TaskBoardWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));

    const instructions = await screen.findByLabelText(/instructions/i);
    fireEvent.change(instructions, { target: { value: "Irgendwas erledigen." } });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(vi.mocked(toast.error).mock.calls[0][0]).toMatch(/no team lead/i);
  });

  it("draws its title once and its columns have no border of their own", () => {
    vi.spyOn(hooks, "useTaskBoard").mockReturnValue({
      data: [
        {
          id: "t1",
          title: "Tickets",
          state: "waiting",
          column: "waiting",
          departmentId: "dept-1",
          departmentName: "Kundenservice",
          agentId: null,
          agentName: null,
          requestedByMemberId: null,
          parentTaskId: null,
          delegationDepth: 0,
          createdAt: "2026-11-01T00:00:00Z",
        },
      ],
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useDepartments").mockReturnValue({
      data: { items: departments, totalCount: 2 },
      isPending: false,
    } as never);
    vi.spyOn(hooks, "useCreateTask").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never);
    const { container } = render(<TaskBoardWidget config={{}} onConfigChange={vi.fn()} />, {
      wrapper,
    });
    expect(screen.getAllByText("Task Board")).toHaveLength(1);
    const cols = container.querySelectorAll("[data-column]");
    expect(cols).toHaveLength(4);
    for (const col of cols) expect(col.className).not.toMatch(/\bborder\b/);
  });
});
