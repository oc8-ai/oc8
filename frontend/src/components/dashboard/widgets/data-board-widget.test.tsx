import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_FILTERS } from "@/components/boards/board-filters";
import { WidgetFrame } from "@/components/dashboard/widget-frame";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import * as boardsApi from "@/lib/boards-api";
import * as hooks from "@/lib/hooks";
import type { Board } from "@/lib/boards-api";
import { DataBoardWidget } from "./data-board-widget";

vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  retryUnlessRefused: () => false,
}));

function board(over: Partial<Board> = {}): Board {
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
    mayRun: true,
    specVersion: 1,
    spec: { datasets: [], blocks: [] },
    ...over,
  };
}

function wrap(
  ui: ReactNode,
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

function mockBoard(answer: { data?: Board; error?: unknown }) {
  vi.spyOn(boardsApi, "useBoard").mockReturnValue({
    data: answer.data,
    error: answer.error ?? null,
    isPending: !answer.data && !answer.error,
    isError: !!answer.error,
  } as never);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.delete).mockReset();
});

describe("DataBoardWidget", () => {
  it.each([404, 403])("shows No access and never the title on %i", (status) => {
    mockBoard({ error: new ApiError("Sales", status) });
    render(wrap(<DataBoardWidget config={{ boardId: "b1" }} onConfigChange={vi.fn()} />));
    expect(screen.getByText("No access")).toBeInTheDocument();
    expect(screen.queryByText("Sales")).toBeNull();
    expect(screen.queryByText(/removed/)).toBeNull();
  });

  it.each([{}, { boardId: 42 }, { boardId: "" }])(
    "says a tile without a usable board id can't be shown (%o)",
    (config) => {
      mockBoard({});
      render(wrap(<DataBoardWidget config={config} onConfigChange={vi.fn()} />));
      expect(screen.getByText("This dashboard can't be shown.")).toBeInTheDocument();
      expect(screen.queryByText("No access")).toBeNull();
    },
  );

  it("shows removed on 410", () => {
    mockBoard({ error: new ApiError("gone", 410) });
    render(wrap(<DataBoardWidget config={{ boardId: "b1" }} onConfigChange={vi.fn()} />));
    expect(screen.getByText("This dashboard was removed.")).toBeInTheDocument();
    expect(screen.queryByText("No access")).toBeNull();
  });

  it("shows the no-longer-updated banner for a stale board", () => {
    mockBoard({ data: board({ stale: true }) });
    render(wrap(<DataBoardWidget config={{ boardId: "b1" }} onConfigChange={vi.fn()} />));
    expect(screen.getByText("Sales")).toBeInTheDocument();
    expect(screen.getByText("No longer updated")).toBeInTheDocument();
  });

  it("shows the title and no banner for a live board", () => {
    mockBoard({ data: board() });
    render(wrap(<DataBoardWidget config={{ boardId: "b1" }} onConfigChange={vi.fn()} />));
    expect(screen.getByText("Sales")).toBeInTheDocument();
    expect(screen.queryByText("No longer updated")).toBeNull();
  });

  it("adds no side padding of its own, so block titles line up with the tile's", () => {
    mockBoard({ data: board() });
    render(wrap(<DataBoardWidget config={{ boardId: "b1" }} onConfigChange={vi.fn()} />));
    expect(screen.getByTestId("board-body").className).not.toMatch(/(^|\s)(px|pl|pr)-/);
  });

  it("stores filter changes in the tile config", () => {
    mockBoard({
      data: board({
        spec: {
          datasets: [],
          blocks: [{ type: "filter_bar", width: 12, presets: ["this_month", "year_to_date"] }],
        },
      }),
    });
    const onConfigChange = vi.fn();
    render(wrap(<DataBoardWidget config={{ boardId: "b1" }} onConfigChange={onConfigChange} />));
    fireEvent.change(screen.getByLabelText("Period"), { target: { value: "year_to_date" } });
    expect(onConfigChange).toHaveBeenCalledWith({
      boardId: "b1",
      filters: { ...DEFAULT_FILTERS, preset: "year_to_date" },
    });
  });

  it("reads saved filters from the tile config", () => {
    mockBoard({
      data: board({
        spec: {
          datasets: [],
          blocks: [{ type: "filter_bar", width: 12, presets: ["this_month", "year_to_date"] }],
        },
      }),
    });
    render(
      wrap(
        <DataBoardWidget
          config={{ boardId: "b1", filters: { ...DEFAULT_FILTERS, preset: "year_to_date" } }}
          onConfigChange={vi.fn()}
        />,
      ),
    );
    expect(screen.getByLabelText("Period")).toHaveValue("year_to_date");
  });
});

describe("data_board refresh", () => {
  type Run = { state: string } | undefined;
  function mockRun(opts: { pending?: boolean; run?: () => Run } = {}) {
    const mutate = vi.fn();
    vi.spyOn(hooks, "useRunAgent").mockReturnValue({
      mutate,
      isPending: opts.pending ?? false,
    } as never);
    const useRun = vi
      .spyOn(hooks, "useRun")
      .mockImplementation(
        (runId: string | null) => ({ data: runId ? opts.run?.() : undefined }) as never,
      );
    return { mutate, useRun };
  }
  const widget = <DataBoardWidget config={{ boardId: "b1" }} onConfigChange={vi.fn()} />;

  it.each([
    ["the person may not run the agent", { mayRun: false }],
    ["no agent fills the board", { agentId: null }],
    ["the board is no longer updated", { stale: true }],
  ])("offers no refresh when %s", (_why, over) => {
    mockBoard({ data: board(over) });
    mockRun();
    render(wrap(widget));
    expect(screen.queryByRole("button", { name: "Refresh data" })).toBeNull();
  });

  it("asks the board's agent to refresh it", () => {
    mockBoard({ data: board() });
    const { mutate } = mockRun();
    render(wrap(widget));
    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    expect(mutate).toHaveBeenCalledWith(
      { agentId: "a1", task: 'Refresh the dashboard "Sales" with current data.' },
      expect.anything(),
    );
  });

  it("is disabled while the run is starting", () => {
    mockBoard({ data: board() });
    mockRun({ pending: true });
    render(wrap(widget));
    expect(screen.getByRole("button", { name: "Refresh data" })).toBeDisabled();
  });

  it("stays disabled until the run ends, then reloads the board", async () => {
    mockBoard({ data: board() });
    let run: Run = { state: "running" };
    const { mutate } = mockRun({ run: () => run });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    const view = render(wrap(widget, qc));

    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    act(() => mutate.mock.calls[0][1].onSuccess({ id: "r1" }));
    const button = screen.getByRole("button", { name: "Refresh data" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "Updating…");
    expect(invalidate).not.toHaveBeenCalled();

    run = { state: "failed" };
    // A new element: the same one would let React skip the render.
    view.rerender(
      wrap(<DataBoardWidget config={{ boardId: "b1" }} onConfigChange={vi.fn()} />, qc),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled());
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["boards", "b1"] });
    expect(toast.error).toHaveBeenCalledWith("Couldn't refresh");
  });
});

describe("data_board tile menu", () => {
  function frame(config: Record<string, unknown>) {
    return (
      <WidgetFrame
        instance={{ id: "w1", type: "data_board", x: 0, y: 0, w: 6, h: 10, config }}
        editing
        onConfigChange={vi.fn()}
        onRemove={vi.fn()}
      />
    );
  }

  it("offers no delete when the person may not delete", () => {
    mockBoard({ data: board({ mayDelete: false }) });
    render(wrap(frame({ boardId: "b1" })));
    expect(screen.queryByRole("button", { name: "Delete dashboard" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove widget" })).toBeInTheDocument();
  });

  it("removing the tile never deletes the board", () => {
    mockBoard({ data: board() });
    const onRemove = vi.fn();
    render(
      wrap(
        <WidgetFrame
          instance={{
            id: "w1",
            type: "data_board",
            x: 0,
            y: 0,
            w: 6,
            h: 10,
            config: { boardId: "b1" },
          }}
          editing
          onConfigChange={vi.fn()}
          onRemove={onRemove}
        />,
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove widget" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(api.delete).not.toHaveBeenCalled();
  });

  it("deletes after confirming and never refetches the deleted board", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/boards/b1") return board();
      return [];
    });
    vi.mocked(api.delete).mockResolvedValue(undefined);

    const view = render(wrap(frame({ boardId: "b1" }), qc));
    expect(await screen.findByText("Sales")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete dashboard" }));
    expect(screen.getByText('Delete "Sales" and its 2 records?')).toBeInTheDocument();
    expect(api.delete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/boards/b1?records=2"));
    expect(await screen.findByText("This dashboard was removed.")).toBeInTheDocument();

    // A re-render after the delete (the grid re-renders on every layout
    // change) must not bring the deleted board back by fetching it.
    view.rerender(wrap(frame({ boardId: "b1" }), qc));
    await new Promise((r) => setTimeout(r, 20));
    expect(vi.mocked(api.get).mock.calls.filter(([p]) => p === "/boards/b1")).toHaveLength(1);
    expect(screen.getByText("This dashboard was removed.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete dashboard" })).toBeNull();
  });
});
