import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as notificationsApi from "@/lib/notifications-api";
import { BoardNotices } from "./board-notices";

const item = (id: string, boardId: string) => ({
  id,
  kind: "board.available",
  subjectId: boardId,
  title: `New dashboard ${id}`,
  body: `Body ${id}`,
  link: `/boards/${boardId}`,
  createdAt: new Date().toISOString(),
  read: false,
});

function setup(items: ReturnType<typeof item>[], onGrid: string[] = []) {
  const spy = vi
    .spyOn(notificationsApi, "useNotifications")
    .mockReturnValue({ data: items } as never);
  const mutate = vi.fn();
  vi.spyOn(notificationsApi, "useMarkRead").mockReturnValue({ mutate } as never);
  const onAdd = vi.fn();
  render(<BoardNotices onAdd={onAdd} boardIdsOnGrid={onGrid} />);
  return { spy, mutate, onAdd };
}

afterEach(() => vi.restoreAllMocks());

describe("BoardNotices", () => {
  it("asks for unread board.available notifications", () => {
    const { spy } = setup([item("n1", "b1")]);
    expect(spy).toHaveBeenCalledWith({ unread: true, kind: "board.available" });
    expect(screen.getByText("New dashboard n1")).toBeInTheDocument();
  });

  it("renders nothing without notices", () => {
    setup([]);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows at most 3 and counts the rest", () => {
    setup([
      item("n1", "b1"),
      item("n2", "b2"),
      item("n3", "b3"),
      item("n4", "b4"),
      item("n5", "b5"),
    ]);
    expect(screen.getAllByRole("button", { name: "Add to My work" })).toHaveLength(3);
    expect(screen.getByText("+2 more")).toBeInTheDocument();
  });

  it("adds the tile and marks the notice read", () => {
    const { onAdd, mutate } = setup([item("n1", "b1")]);
    fireEvent.click(screen.getByRole("button", { name: "Add to My work" }));
    expect(onAdd).toHaveBeenCalledWith("data_board", { boardId: "b1" });
    expect(mutate).toHaveBeenCalledWith("n1");
  });

  it("dismisses by marking read, without adding", () => {
    const { onAdd, mutate } = setup([item("n1", "b1")]);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(mutate).toHaveBeenCalledWith("n1");
    expect(onAdd).not.toHaveBeenCalled();
  });

  it("marks a notice read when its board is already on the grid", () => {
    const { mutate } = setup([item("n1", "b1"), item("n2", "b2")], ["b1"]);
    expect(mutate).toHaveBeenCalledWith("n1");
    expect(mutate).not.toHaveBeenCalledWith("n2");
    expect(screen.queryByText("New dashboard n1")).toBeNull();
  });
});
