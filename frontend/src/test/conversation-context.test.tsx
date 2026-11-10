// frontend/src/test/conversation-context.test.tsx
// C3 pinned context: the pinned bar, the `#`/+ search dialog across kinds and
// the data source browser.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";

const setPins = vi.hoisted(() => vi.fn());
const searching = vi.hoisted(() => ({ current: false }));
const browse = vi.hoisted(() => ({
  current: { data: undefined, error: { status: 409 } } as {
    data?: unknown;
    error?: unknown;
    isLoading?: boolean;
  },
}));
const browseCalls = vi.hoisted(() => [] as Array<[string | null, string]>);
const HITS = vi.hoisted(() => [
  { kind: "knowledge_base", id: "kb1", label: "Prices", hint: undefined },
  { kind: "data_source", id: "ds1", label: "Drive", hint: undefined, connected: false },
  { kind: "agent", id: "a1", label: "Lennart", hint: undefined },
  { kind: "data_source", id: "ds2", label: "Share", hint: undefined, connected: true },
]);
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useContextSearch: () => HITS,
  useContextSearchResults: () =>
    searching.current ? { items: [], isLoading: true } : { items: HITS, isLoading: false },
  useBrowseSource: (sourceId: string | null, path: string) => {
    browseCalls.push([sourceId, path]);
    return browse.current;
  },
}));

import { ContextSearchDialog } from "@/components/conversation/context-search";
import { DataSourceBrowser } from "@/components/conversation/data-source-browser";
import { PinnedBar } from "@/components/conversation/pinned-bar";
import { pinErrorText } from "@/lib/conversation-api";

function wrap(ui: React.ReactElement) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

const PINS = [
  { kind: "knowledge_base", id: "kb1", label: "Prices", available: true },
  { kind: "knowledge_base", id: "kb2", label: "Board minutes", available: false },
];

const pinning = { mutate: setPins, isPending: false };
const t = (en: string) => en;

beforeEach(() => {
  setPins.mockReset();
  searching.current = false;
  browseCalls.length = 0;
  browse.current = { data: undefined, error: { status: 409 } };
});

describe("pinned context", () => {
  it("shows pins, marks unavailable ones and removes on click", () => {
    wrap(<PinnedBar pins={PINS} pinning={pinning} />);
    expect(screen.getByText("Pinned")).toBeInTheDocument();
    expect(screen.getByText("Source not available")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Unpin Prices" }));
    expect(setPins).toHaveBeenCalledWith({ remove: { kind: "knowledge_base", id: "kb1" } });
  });

  it("lets an unavailable pin be removed too", () => {
    wrap(<PinnedBar pins={PINS} pinning={pinning} />);
    fireEvent.click(screen.getByRole("button", { name: "Unpin Board minutes" }));
    expect(setPins).toHaveBeenCalledWith({ remove: { kind: "knowledge_base", id: "kb2" } });
  });

  it("shows pins without remove buttons when read-only", () => {
    wrap(<PinnedBar pins={PINS} pinning={pinning} readOnly />);
    expect(screen.getByText("Prices")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Unpin/ })).toBeNull();
  });

  it("renders nothing without pins", () => {
    const { container } = wrap(<PinnedBar pins={[]} pinning={pinning} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("disables every remove button while a pin write runs", () => {
    wrap(<PinnedBar pins={PINS} pinning={{ mutate: setPins, isPending: true }} />);
    for (const button of screen.getAllByRole("button", { name: /Unpin/ })) {
      expect(button).toBeDisabled();
    }
  });

  it("names the refused pins of a 422 by their labels", () => {
    const error = new ApiError(
      JSON.stringify({
        message: "not available to you",
        refused: [{ kind: "knowledge_base", id: "kb2" }],
      }),
      422,
    );
    expect(pinErrorText(error, PINS, t)).toContain("Board minutes");
    expect(pinErrorText(new ApiError("boom", 500), PINS, t)).toBe(
      "Your pins were not saved. Try again.",
    );
    expect(pinErrorText(null, PINS, t)).toBeNull();
  });

  it("renders a label as plain text, never as markup", () => {
    wrap(
      <PinnedBar
        pinning={pinning}
        pins={[{ kind: "agent", id: "a", label: "<b>bold</b>", available: true }]}
      />,
    );
    expect(screen.getByText("<b>bold</b>")).toBeInTheDocument();
  });
});

describe("ContextSearchDialog", () => {
  it("searches every kind with type icons and offers Connect for an unconnected source", () => {
    const onPick = vi.fn();
    wrap(<ContextSearchDialog open onPick={onPick} onClose={vi.fn()} />);
    expect(screen.getByRole("link", { name: /Connect/ })).toHaveAttribute("href", "/knowledge");
    fireEvent.click(screen.getByRole("option", { name: /Lennart/ }));
    expect(onPick).toHaveBeenCalledWith({
      kind: "agent",
      id: "a1",
      label: "Lennart",
      hint: undefined,
    });
  });

  it("works by keyboard: arrows skip an unconnected source, Enter picks", () => {
    const onPick = vi.fn();
    wrap(<ContextSearchDialog open onPick={onPick} onClose={vi.fn()} />);
    const box = screen.getByRole("combobox", { name: "Search" });
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ id: "a1" }));
  });

  it("wraps around with ArrowUp", () => {
    const onPick = vi.fn();
    wrap(<ContextSearchDialog open onPick={onPick} onClose={vi.fn()} />);
    const box = screen.getByRole("combobox", { name: "Search" });
    fireEvent.keyDown(box, { key: "ArrowUp" });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ id: "ds2" }));
  });

  it("points the field at the active option", () => {
    wrap(<ContextSearchDialog open onPick={vi.fn()} onClose={vi.fn()} />);
    const box = screen.getByRole("combobox", { name: "Search" });
    fireEvent.keyDown(box, { key: "ArrowDown" });
    const active = screen.getByRole("option", { name: /Lennart/ });
    expect(active).toHaveAttribute("aria-selected", "true");
    expect(box).toHaveAttribute("aria-activedescendant", active.id);
    // The unconnected source is no option at all.
    expect(screen.queryByRole("option", { name: /Drive/ })).toBeNull();
  });

  it("shows a skeleton while the first results load", () => {
    searching.current = true;
    wrap(<ContextSearchDialog open onPick={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getAllByTestId("search-skeleton")).toHaveLength(3);
    expect(screen.queryByText(/Nothing you can open/)).toBeNull();
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    wrap(<ContextSearchDialog open onPick={vi.fn()} onClose={onClose} />);
    fireEvent.keyDown(screen.getByRole("combobox", { name: "Search" }), { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});

describe("DataSourceBrowser", () => {
  it("lists only connected sources as browsable and offers Connect otherwise", () => {
    wrap(<DataSourceBrowser open onPick={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole("link", { name: /Connect/ })).toHaveAttribute("href", "/knowledge");
    expect(screen.getByRole("button", { name: /Share/ })).toBeInTheDocument();
  });

  it("offers to pin a source that can't be browsed as a whole", () => {
    const onPick = vi.fn();
    wrap(<DataSourceBrowser open onPick={onPick} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Share/ }));
    expect(
      screen.getByText("This source can't be browsed — pin it as a whole."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pin whole source" }));
    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "data_source", id: "ds2", label: "Share" }),
    );
  });

  it("browses folders with breadcrumbs and picks a file as a source_file ref", () => {
    browse.current = {
      data: {
        entries: [
          { id: "docs", name: "Docs", isFolder: true, size: null, contentType: null },
          { id: "a:b.txt", name: "b.txt", isFolder: false, size: 3, contentType: "text/plain" },
        ],
        truncated: true,
      },
      error: null,
    };
    const onPick = vi.fn();
    wrap(<DataSourceBrowser open onPick={onPick} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Share/ }));
    expect(screen.getByText("More entries exist – search more specifically")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Docs/ }));
    expect(browseCalls.at(-1)).toEqual(["ds2", "docs"]);
    const crumbs = screen.getByRole("navigation", { name: "Folders" });
    fireEvent.click(screen.getAllByRole("button", { name: "Share" })[0]);
    expect(browseCalls.at(-1)).toEqual(["ds2", "/"]);
    expect(crumbs).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /b\.txt/ }));
    expect(onPick).toHaveBeenCalledWith({
      kind: "source_file",
      id: "ds2:a:b.txt",
      label: "b.txt",
    });
  });

  it("shows a connector failure inline", () => {
    browse.current = { data: undefined, error: new ApiError("The source did not answer.", 502) };
    wrap(<DataSourceBrowser open onPick={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Share/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("The source did not answer.");
  });
});
