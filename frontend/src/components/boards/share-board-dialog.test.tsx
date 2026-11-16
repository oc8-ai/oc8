import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as boardsApi from "@/lib/boards-api";
import type { BoardSummary } from "@/lib/boards-api";
import { ShareBoardDialog } from "./share-board-dialog";

const m = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: m.error, success: vi.fn() } }));

const board: BoardSummary = {
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
  mayRun: false,
  mayShare: true,
  sharedWithMe: false,
};

function setup() {
  const shares = vi.spyOn(boardsApi, "useBoardShares").mockReturnValue({
    data: [
      {
        id: "s1",
        memberId: "m9",
        memberName: "Eva Example",
        departmentId: null,
        departmentName: null,
        createdAt: "",
      },
    ],
  } as never);
  const targets = vi.spyOn(boardsApi, "useShareTargets").mockReturnValue({
    data: {
      members: [{ id: "m1", name: "Max Muster" }],
      departments: [{ id: "d2", name: "Finance" }],
    },
  } as never);
  const share = vi.fn();
  const unshare = vi.fn();
  vi.spyOn(boardsApi, "useShareBoard").mockReturnValue({
    mutate: share,
    isPending: false,
  } as never);
  vi.spyOn(boardsApi, "useUnshareBoard").mockReturnValue({
    mutate: unshare,
    isPending: false,
  } as never);
  render(<ShareBoardDialog board={board} open onOpenChange={vi.fn()} />);
  return { shares, targets, share, unshare };
}

// cmdk scrolls the active item into view; jsdom has no layout.
Element.prototype.scrollIntoView = () => {};

afterEach(() => {
  vi.restoreAllMocks();
  m.error.mockReset();
});

describe("ShareBoardDialog", () => {
  it("searches the targets as the person types", () => {
    const { targets } = setup();
    fireEvent.change(screen.getByPlaceholderText("Search people and departments"), {
      target: { value: "ma" },
    });
    expect(targets).toHaveBeenLastCalledWith("b1", "ma", true);
  });

  it("groups members and departments", () => {
    setup();
    expect(screen.getByText("People")).toBeInTheDocument();
    expect(screen.getByText("Departments")).toBeInTheDocument();
  });

  it("shares with a member", () => {
    const { share } = setup();
    fireEvent.click(screen.getByText("Max Muster"));
    expect(share.mock.calls[0][0]).toEqual({ id: "b1", memberId: "m1" });
  });

  it("shares with a department", () => {
    const { share } = setup();
    fireEvent.click(screen.getByText("Finance"));
    expect(share.mock.calls[0][0]).toEqual({ id: "b1", departmentId: "d2" });
  });

  it("lists the current shares and removes one", () => {
    const { unshare } = setup();
    expect(screen.getByText("Eva Example")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove Eva Example" }));
    expect(unshare.mock.calls[0][0]).toEqual({ id: "b1", shareId: "s1" });
  });

  it("toasts when removing a share fails", () => {
    const { unshare } = setup();
    unshare.mockImplementation((_v: unknown, o: { onError?: () => void }) => o.onError?.());
    fireEvent.click(screen.getByRole("button", { name: "Remove Eva Example" }));
    expect(m.error).toHaveBeenCalledWith("Couldn't remove the share");
  });

  it("toasts when sharing fails", () => {
    const { share } = setup();
    share.mockImplementation((_v: unknown, o: { onError?: () => void }) => o.onError?.());
    fireEvent.click(screen.getByText("Max Muster"));
    expect(m.error).toHaveBeenCalledWith("Couldn't share the dashboard");
  });
});
