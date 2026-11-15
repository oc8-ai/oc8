// Task 14: the `boardBlocks` edition slot renders artifact blocks in Enterprise.
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const caps = vi.hoisted(() => ({ value: null as string[] | null }));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => caps.value,
  useCan: () => () => true,
}));
const post = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { post },
}));

import { EditionCompositionProvider, composeFrontendEdition } from "@/edition/composition";
import type { BoardBlockSlotProps, FrontendEditionExtension } from "@/edition/contracts";
import { EditionBoardBlock } from "@/edition/slots";
import type { BoardQuery, QueryResult } from "@/lib/boards-api";

const block = { type: "artifact", artifact_ref: "art-1", datasets: ["sales"] };

function Probe({ boardId, block: b }: BoardBlockSlotProps) {
  return (
    <p>
      probe {boardId} {String(b.artifact_ref)}
    </p>
  );
}

const ext = (component = Probe): FrontendEditionExtension => ({
  id: "ent",
  boardBlocks: [{ id: "canvas", needs: ["enterprise.canvas"], component }],
});

function show(extensions: readonly FrontendEditionExtension[]) {
  return render(
    <EditionCompositionProvider extensions={extensions}>
      <EditionBoardBlock boardId="b1" block={block} />
    </EditionCompositionProvider>,
  );
}

describe("edition board blocks", () => {
  beforeEach(() => {
    caps.value = null;
    post.mockReset();
  });

  it("renders the placeholder in Community", () => {
    show([]);
    expect(screen.getByText(/Available in Enterprise/)).toBeInTheDocument();
  });

  it("renders the edition block when the caller holds enterprise.canvas", () => {
    caps.value = ["enterprise.canvas"];
    show([ext()]);
    expect(screen.getByText("probe b1 art-1")).toBeInTheDocument();
    expect(screen.queryByText(/Available in Enterprise/)).toBeNull();
  });

  it("falls back to the placeholder when the capability is missing", () => {
    caps.value = ["something.else"];
    show([ext()]);
    expect(screen.queryByText(/probe/)).toBeNull();
    expect(screen.getByText(/Available in Enterprise/)).toBeInTheDocument();
  });

  it("falls back to the placeholder when the edition block throws", () => {
    caps.value = ["enterprise.canvas"];
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    show([
      ext(() => {
        throw new Error("boom");
      }),
    ]);
    expect(screen.getByText(/Available in Enterprise/)).toBeInTheDocument();
    spy.mockRestore();
  });

  it("rejects duplicate board block ids", () => {
    const entry = { id: "canvas", component: Probe };
    expect(() =>
      composeFrontendEdition([
        { id: "one", boardBlocks: [entry] },
        { id: "two", boardBlocks: [entry] },
      ]),
    ).toThrow("Duplicate edition board block: canvas");
  });

  it("hands the block a query function that posts to the board's query route", async () => {
    caps.value = ["enterprise.canvas"];
    const result = { rows: [] } as unknown as QueryResult;
    post.mockResolvedValue(result);
    const q: BoardQuery = {
      dataset: "sales",
      fields: ["amount"],
      period: { preset: "this_month" } as BoardQuery["period"],
    };
    let got: QueryResult | null = null;
    function Caller({ query }: BoardBlockSlotProps) {
      void query(q).then((r) => {
        got = r;
      });
      return <p>asked</p>;
    }
    show([ext(Caller)]);
    await waitFor(() => expect(got).toBe(result));
    expect(post).toHaveBeenCalledWith("/boards/b1/query", q);
  });

  it("passes the block read-only and never as markup", () => {
    caps.value = ["enterprise.canvas"];
    show([ext(({ block: b }) => <p data-testid="raw">{JSON.stringify(b)}</p>)]);
    expect(screen.getByTestId("raw").textContent).toBe(JSON.stringify(block));
  });
});
