// frontend/src/test/department-private-board.test.tsx
// A member's personal department read by someone else (`contentHidden` on the
// department): its task board is the owner's alone and the API answers it
// with a 404. The page says so instead of showing an empty board, does not
// ask for the board at all, and a 404 is never retried.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => {
  const actual = await orig<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { get, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  };
});

import { ApiError } from "@/lib/api";
import { useDepartmentBoard } from "@/lib/hooks";
import { DepartmentTaskBoard } from "@/routes/departments.$id";

const DEPT_ID = "dept-personal";
const NOTICE = /only that member can see its tasks/i;

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function requestedBoard(): boolean {
  return get.mock.calls.some(([path]) => String(path).startsWith(`/departments/${DEPT_ID}/board`));
}

beforeEach(() => {
  get.mockReset();
  get.mockImplementation(() =>
    Promise.resolve({
      tasks: [{ id: "t1", title: "Answer the landlord", column: "backlog", agentId: null }],
      totals: { backlog: 1 },
    }),
  );
});

describe("a personal department's task board", () => {
  it("shows the private notice and does not request the board when contentHidden", async () => {
    wrap(<DepartmentTaskBoard departmentId={DEPT_ID} contentHidden members={[]} />);
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requestedBoard()).toBe(false);
    expect(screen.queryByText("Backlog")).toBeNull();
  });

  it("the owner sees the board as before", async () => {
    wrap(<DepartmentTaskBoard departmentId={DEPT_ID} contentHidden={false} members={[]} />);
    expect(await screen.findByText("Answer the landlord")).toBeInTheDocument();
    expect(requestedBoard()).toBe(true);
    expect(screen.queryByText(NOTICE)).toBeNull();
  });

  it("does not retry a 404", async () => {
    get.mockImplementation(() => Promise.reject(new ApiError("department not found", 404)));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: 3, retryDelay: 0 } } });
    const { result } = renderHook(() => useDepartmentBoard(DEPT_ID), {
      wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(get).toHaveBeenCalledTimes(1);
  });
});
