import type { ReactNode } from "react";

import { WidgetTitle } from "@/components/dashboard/widget-title";
import type { ArtifactBlock, Board, BoardBlock, BlockWidth } from "@/lib/boards-api";
import { useT } from "@/lib/i18n";

import { BlockMessage } from "./block-status";
import { blockQuery, type BoardFilterState } from "./board-filters";
import { ChartBlock } from "./chart-block";
import { FilterBar } from "./filter-bar";
import { KpiBlock } from "./kpi-block";
import { NoteBlock } from "./note-block";
import { TableBlock } from "./table-block";

// Static class names, so Tailwind sees each one. On a phone every block is
// full width; from `md` up a block takes its `width` of 12 columns.
const SPAN: Record<BlockWidth, string> = {
  3: "md:col-span-3",
  4: "md:col-span-4",
  6: "md:col-span-6",
  8: "md:col-span-8",
  12: "md:col-span-12",
};

/** The neutral stand-in for an `artifact` block, which core never renders
 *  itself. The edition slot replaces this one component. */
export function ArtifactBlockPlaceholder({ block }: { block: ArtifactBlock }) {
  const t = useT();
  return (
    <div className="flex h-full flex-col">
      {block.title ? <WidgetTitle title={block.title} /> : null}
      <BlockMessage>{t("Available in Enterprise", "Verfügbar in Enterprise")}</BlockMessage>
    </div>
  );
}

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
    <div className="grid min-w-0 grid-cols-12 gap-x-3 gap-y-2">
      {spec.blocks.map((block, i) => (
        <div
          key={i}
          data-testid="board-block"
          className={`col-span-12 min-w-0 ${SPAN[block.width ?? 6] ?? SPAN[12]}`}
        >
          {renderBlock(block, board, filters, onFiltersChange, today)}
        </div>
      ))}
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
  if (block.type === "artifact") return <ArtifactBlockPlaceholder block={block} />;

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
