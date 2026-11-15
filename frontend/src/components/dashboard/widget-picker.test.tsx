import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as boardsApi from "@/lib/boards-api";
import type { BoardSummary } from "@/lib/boards-api";
import { WidgetPicker } from "./widget-picker";

function mockBoards(list: BoardSummary[]) {
  vi.spyOn(boardsApi, "useBoards").mockReturnValue({ data: list } as never);
}

const sales: BoardSummary = {
  id: "b1",
  title: "Sales",
  description: "",
  departmentId: "d1",
  agentId: "a1",
  agentName: "Sales agent",
  stale: false,
  recordCount: 2,
  lastRecordedAt: null,
  mayDelete: false,
};

function open() {
  fireEvent.pointerDown(screen.getByRole("button", { name: /add widget/i }), { button: 0 });
}

afterEach(() => vi.restoreAllMocks());

describe("WidgetPicker", () => {
  it("calls onAdd with the picked widget type", () => {
    mockBoards([]);
    const onAdd = vi.fn();
    render(<WidgetPicker onAdd={onAdd} />);
    open();
    fireEvent.click(screen.getByText("Budget"));
    expect(onAdd).toHaveBeenCalledWith("budget");
  });

  it("offers visible dashboards in their own section", () => {
    mockBoards([sales]);
    const onAdd = vi.fn();
    render(<WidgetPicker onAdd={onAdd} />);
    open();
    expect(screen.getByText("Dashboards")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Sales"));
    expect(onAdd).toHaveBeenCalledWith("data_board", { boardId: "b1" });
  });

  it("never lists data_board as a generic widget", () => {
    mockBoards([]);
    render(<WidgetPicker onAdd={vi.fn()} />);
    open();
    expect(screen.getAllByRole("menuitem")).toHaveLength(7);
    expect(screen.queryByText("Dashboard")).toBeNull();
    expect(screen.queryByText("Dashboards")).toBeNull();
  });
});
