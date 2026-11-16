import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api";
import * as notificationsApi from "@/lib/notifications-api";
import { BoardNotices } from "./board-notices";

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn() },
  retryUnlessRefused: () => false,
}));

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
  // Never settles: the notice stays mounted while the read mark is in flight.
  const mutateAsync = vi.fn(() => new Promise<void>(() => {}));
  vi.spyOn(notificationsApi, "useMarkRead").mockReturnValue({ mutate, mutateAsync } as never);
  const onAdd = vi.fn();
  render(<BoardNotices onAdd={onAdd} boardIdsOnGrid={onGrid} />);
  return { spy, mutate, mutateAsync, onAdd };
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
    const { onAdd, mutateAsync } = setup([item("n1", "b1")]);
    fireEvent.click(screen.getByRole("button", { name: "Add to My work" }));
    expect(onAdd).toHaveBeenCalledWith("data_board", { boardId: "b1" });
    expect(mutateAsync).toHaveBeenCalledWith("n1");
  });

  it("adds once on a double click while the notice is still mounted", () => {
    const { onAdd, mutateAsync } = setup([item("n1", "b1")]);
    const add = screen.getByRole("button", { name: "Add to My work" });
    fireEvent.click(add);
    expect(add.isConnected).toBe(true);
    fireEvent.click(add);
    expect(onAdd).toHaveBeenCalledTimes(1);
    expect(mutateAsync).toHaveBeenCalledTimes(1);
  });

  it("marks a redundant notice once per mount even when the mark keeps failing", async () => {
    vi.restoreAllMocks();
    vi.mocked(api.get).mockResolvedValue({
      items: [
        {
          id: "n1",
          kind: "board.available",
          subjectId: "b1",
          title: { en: "t", de: "t" },
          body: { en: "b", de: "b" },
          link: "/x",
          createdAt: new Date().toISOString(),
          read: false,
        },
      ],
      totalCount: 1,
      nextCursor: null,
    });
    vi.mocked(api.post).mockRejectedValue(new ApiError("forbidden", 403));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ui = () => (
      <QueryClientProvider client={qc}>
        <BoardNotices onAdd={vi.fn()} boardIdsOnGrid={["b1"]} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui());
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    for (let i = 0; i < 4; i++) {
      rerender(ui());
      await new Promise((r) => setTimeout(r, 0));
    }
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it("lets the person retry an add whose read mark failed", async () => {
    const { mutateAsync } = setup([item("n1", "b1")]);
    mutateAsync.mockRejectedValueOnce(new Error("boom"));
    const add = screen.getByRole("button", { name: "Add to My work" });
    fireEvent.click(add);
    await Promise.resolve();
    await Promise.resolve();
    fireEvent.click(add);
    expect(mutateAsync).toHaveBeenCalledTimes(2);
  });

  it("dismisses by marking read, without adding", () => {
    const { onAdd, mutateAsync } = setup([item("n1", "b1")]);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(mutateAsync).toHaveBeenCalledWith("n1");
    expect(onAdd).not.toHaveBeenCalled();
  });

  it("marks a notice read when its board is already on the grid", () => {
    const { mutate } = setup([item("n1", "b1"), item("n2", "b2")], ["b1"]);
    expect(mutate).toHaveBeenCalledWith("n1");
    expect(mutate).not.toHaveBeenCalledWith("n2");
    expect(screen.queryByText("New dashboard n1")).toBeNull();
  });
});
