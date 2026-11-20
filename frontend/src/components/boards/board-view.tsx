import type { ReactNode } from "react";

import { EditionBoardBlock } from "@/edition/slots";
import type { Board, BoardBlock, BlockWidth } from "@/lib/boards-api";

import { blockQuery, type BoardFilterState } from "./board-filters";
import { ChartBlock } from "./chart-block";
import { FilterBar } from "./filter-bar";
import { KpiBlock } from "./kpi-block";
import { NoteBlock } from "./note-block";
import { TableBlock } from "./table-block";

// Static class names, so Tailwind sees each one. Sized by the board's own
// width (a container query), not the viewport's: in a narrow dashboard tile or
// on a phone every block is full width; from 48rem of board up (the old `md`)
// a block takes its `width` of 12 columns.
const SPAN: Record<BlockWidth, string> = {
  3: "@3xl:col-span-3",
  4: "@3xl:col-span-4",
  6: "@3xl:col-span-6",
  8: "@3xl:col-span-8",
  12: "@3xl:col-span-12",
};

/** A dashboard's blocks on a 12-column grid, all reading the same filters. */
export function BoardView({
  board,
  filters,
  onFiltersChange,
  today = new Date(),
}: {
  board: Board;
  filters: BoardFilterState;
  onFiltersChange: (s: BoardFilterState) => void;
  today?: Date;
}) {
  const { spec } = board;
  return (
    <div className="@container min-w-0">
      <div className="grid min-w-0 grid-cols-12 gap-3 px-3">
        {spec.blocks.map((block, i) => (
          <div
            key={i}
            data-testid="board-block"
            className={`col-span-12 min-w-0 ${SPAN[block.width ?? 6] ?? SPAN[12]}`}
          >
            {block.type === "filter_bar" ? (
              renderBlock(block, board, filters, onFiltersChange, today)
            ) : (
              // Each data block on its own quiet card: a fill, not a border,
              // so the tile frame stays the only bordered box.
              <div data-block-card className="h-full min-w-0 rounded-lg bg-muted/50">
                {renderBlock(block, board, filters, onFiltersChange, today)}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function renderBlock(
  block: BoardBlock,
  board: Board,
  filters: BoardFilterState,
  onFiltersChange: (s: BoardFilterState) => void,
  today: Date,
): ReactNode {
  const { spec } = board;
  if (block.type === "filter_bar") {
    return (
      <FilterBar
        boardId={board.id}
        block={block}
        spec={spec}
        filters={filters}
        onFiltersChange={onFiltersChange}
        today={today}
      />
    );
  }
  if (block.type === "artifact")
    return <EditionBoardBlock boardId={board.id} block={{ ...block }} />;

  const dataset = spec.datasets.find((d) => d.key === block.dataset);
  if (!dataset) return null;
  const query = blockQuery(block, spec, filters, today);
  const props = { boardId: board.id, dataset, query };
  switch (block.type) {
    case "kpi":
      return <KpiBlock {...props} block={block} />;
    case "line":
    case "bar":
    case "area":
      return <ChartBlock {...props} block={block} />;
    case "table":
      return <TableBlock {...props} block={block} />;
    case "note":
      return <NoteBlock {...props} block={block} />;
  }
}
