import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import * as boardsApi from "@/lib/boards-api";
import type { BoardSummary } from "@/lib/boards-api";
import { DeleteBoardDialog } from "./delete-board-dialog";

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
};

/** `useDeleteBoard` whose request fails with `error`. */
function failingWith(error: unknown) {
  const mutate = vi.fn((_vars: unknown, opts: { onError?: (e: unknown) => void }) =>
    opts.onError?.(error),
  );
  vi.spyOn(boardsApi, "useDeleteBoard").mockReturnValue({ mutate, isPending: false } as never);
}

function confirm() {
  const onOpenChange = vi.fn();
  render(<DeleteBoardDialog board={board} open onOpenChange={onOpenChange} />);
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" }));
  return onOpenChange;
}

afterEach(() => {
  vi.restoreAllMocks();
  m.error.mockReset();
});

describe("DeleteBoardDialog", () => {
  it("asks to check the count again when new records arrived", () => {
    failingWith(new ApiError("board_records_changed", 409));
    const onOpenChange = confirm();
    expect(m.error).toHaveBeenCalledWith(
      "New records arrived — check the count and confirm again.",
    );
    // Stays open on the refreshed count, for the person to confirm again.
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("keeps the general message for any other failure", () => {
    failingWith(new ApiError("board not found", 404));
    confirm();
    expect(m.error).toHaveBeenCalledWith(
      "The dashboard could not be deleted. Reload and try again.",
    );
  });
});
