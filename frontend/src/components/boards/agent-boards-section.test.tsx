import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as boardsApi from "@/lib/boards-api";
import type { BoardSummary } from "@/lib/boards-api";
import { AgentBoardsSection } from "./agent-boards-section";

function summary(over: Partial<BoardSummary> = {}): BoardSummary {
  return {
    id: "b1",
    title: "Sales",
    description: "",
    departmentId: "d1",
    agentId: "a1",
    agentName: "Sales agent",
    stale: false,
    recordCount: 2,
    lastRecordedAt: null,
    mayDelete: true,
    ...over,
  };
}

function mockBoards(list: BoardSummary[]) {
  const spy = vi
    .spyOn(boardsApi, "useBoards")
    .mockReturnValue({ data: list, isPending: false, isError: false } as never);
  const mutate = vi.fn();
  vi.spyOn(boardsApi, "useDeleteBoard").mockReturnValue({ mutate, isPending: false } as never);
  return { spy, mutate };
}

afterEach(() => vi.restoreAllMocks());

describe("AgentBoardsSection", () => {
  it("lists the agent's dashboards with their record counts", () => {
    const { spy } = mockBoards([summary(), summary({ id: "b2", title: "Stock", recordCount: 7 })]);
    render(<AgentBoardsSection agentId="a1" />);
    expect(spy).toHaveBeenCalledWith("a1");
    expect(screen.getByText("Sales")).toBeInTheDocument();
    expect(screen.getByText("Stock")).toBeInTheDocument();
    expect(screen.getByText("7 records")).toBeInTheDocument();
  });

  it("deletes only after confirming the record count", () => {
    const { mutate } = mockBoards([summary()]);
    render(<AgentBoardsSection agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: 'Delete "Sales"' }));
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText('Delete "Sales" and its 2 records?')).toBeInTheDocument();
    expect(mutate).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0][0]).toEqual({ id: "b1", records: 2 });
  });

  it("offers no delete for a dashboard the person may not delete", () => {
    mockBoards([summary({ mayDelete: false })]);
    render(<AgentBoardsSection agentId="a1" />);
    expect(screen.getByText("Sales")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete/ })).toBeNull();
  });

  it("renders nothing when the agent has no dashboards", () => {
    mockBoards([]);
    const { container } = render(<AgentBoardsSection agentId="a1" />);
    expect(container).toBeEmptyDOMElement();
  });
});
