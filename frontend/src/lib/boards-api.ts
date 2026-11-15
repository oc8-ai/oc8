// Data boards (dashboards) an agent fills. Backend: api/v1/boards.py.
//
// The spec types mirror `oc8.boards.spec` and keep its snake_case keys
// (`split_by`, `page_size`, ...): the spec is stored and served as written.
// The query and its result mirror `oc8.boards.query`, which are CamelModels, so
// their keys are camelCase on the wire -- except a rows-mode row, which is a
// plain dict keyed by field (and `as_of`), not a model.
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, retryUnlessRefused } from "@/lib/api";

export type FieldType = "number" | "money" | "percent" | "text" | "category" | "date";
export type Grain = "day" | "week" | "month" | "moment";
export type BlockWidth = 3 | 4 | 6 | 8 | 12;
export type Preset = "this_month" | "last_12_months" | "year_to_date" | "custom";
export type Comparison = "previous_period" | "same_period_last_year";

export interface FieldSpec {
  key: string;
  label: string;
  type: FieldType;
  /** ISO 4217, set exactly when `type` is `money`. */
  currency?: string | null;
}

export interface DatasetSpec {
  key: string;
  label: string;
  grain: Grain;
  fields: FieldSpec[];
}

interface BlockBase {
  title?: string | null;
  width?: BlockWidth;
}

export interface KpiBlock extends BlockBase {
  type: "kpi";
  dataset: string;
  field: string;
}

export interface ChartBlock extends BlockBase {
  type: "line" | "bar" | "area";
  dataset: string;
  fields: string[];
  split_by?: string | null;
}

export interface TableBlock extends BlockBase {
  type: "table";
  dataset: string;
  fields: string[];
  page_size?: number;
}

export interface CategoryFilterRef {
  dataset: string;
  field: string;
}

export interface FilterBarBlock extends BlockBase {
  type: "filter_bar";
  width?: 12;
  presets?: Preset[];
  comparisons?: Comparison[];
  category_filters?: CategoryFilterRef[];
}

export interface NoteBlock extends BlockBase {
  type: "note";
  dataset: string;
}

export interface ArtifactBlock extends BlockBase {
  type: "artifact";
  artifact_ref: string;
  datasets: string[];
}

export type BoardBlock =
  | KpiBlock
  | ChartBlock
  | TableBlock
  | FilterBarBlock
  | NoteBlock
  | ArtifactBlock;

export interface BoardSpec {
  datasets: DatasetSpec[];
  blocks: BoardBlock[];
}

/** `BoardSummaryDTO`: one dashboard in a list. */
export interface BoardSummary {
  id: string;
  title: string;
  description: string;
  departmentId: string;
  agentId: string | null;
  agentName: string | null;
  /** No producing agent any more: the board is "no longer updated". */
  stale: boolean;
  recordCount: number;
  lastRecordedAt: string | null;
  mayDelete: boolean;
}

/** `DataBoardDTO`: a dashboard with its spec. */
export interface Board extends BoardSummary {
  spec: BoardSpec;
  specVersion: number;
}

/** Half-open: `to` is the first day NOT in the period. */
export interface Period {
  from: string;
  to: string;
}

export type QueryCompare = "none" | Comparison;

export interface BoardQuery {
  dataset: string;
  fields: string[];
  period: Period;
  compare?: QueryCompare;
  filters?: Record<string, string[]>;
  groupBy?: string | null;
  mode?: "aggregate" | "rows";
  reported?: "latest" | "first";
  bucket?: "grain" | "week" | "month" | "year";
  page?: { offset: number; limit: number };
  sort?: { field: string; dir: "asc" | "desc" } | null;
}

/** `asOf` is a date (`YYYY-MM-DD`) for day/week/month data and for month
 *  buckets, and a full ISO datetime for `moment` data at its own grain. */
export interface SeriesPoint {
  asOf: string;
  group: string | null;
  values: Record<string, number | null>;
}

export interface TotalRow {
  group: string | null;
  values: Record<string, number | null>;
}

export interface CompareResult {
  period: Period;
  series: SeriesPoint[];
  totals: TotalRow[];
  /** Relative change per field, as a fraction (0.2 = +20 %); null when
   *  either side is missing or the previous value is zero. */
  change: TotalRow[];
}

export interface BoardNote {
  text: string;
  /** ISO datetime. */
  asOf: string;
  recordedAt: string;
}

export interface QueryResult {
  series: SeriesPoint[];
  totals: TotalRow[];
  compare: CompareResult | null;
  /** Rows mode only. Each row has `as_of` (a full ISO datetime) plus the
   *  requested fields, keyed as in the spec. */
  rows: Record<string, unknown>[] | null;
  totalRows: number | null;
  note: BoardNote | null;
}

export const boardKeys = {
  all: ["boards"] as const,
  list: (agentId?: string) => ["boards", agentId ?? "all"] as const,
  one: (id: string) => ["boards", id] as const,
  query: (id: string, query: BoardQuery | null) => ["boards", id, "query", query] as const,
};

export function useBoards(agentId?: string) {
  return useQuery({
    queryKey: boardKeys.list(agentId),
    queryFn: () =>
      api.get<BoardSummary[]>(
        agentId ? `/boards?agentId=${encodeURIComponent(agentId)}` : "/boards",
      ),
  });
}

export function useBoard(id: string) {
  return useQuery({
    queryKey: boardKeys.one(id),
    queryFn: () => api.get<Board>(`/boards/${encodeURIComponent(id)}`),
    enabled: !!id,
    retry: retryUnlessRefused,
  });
}

export function useBoardQuery(id: string, query: BoardQuery | null) {
  return useQuery({
    queryKey: boardKeys.query(id, query),
    queryFn: () => api.post<QueryResult>(`/boards/${encodeURIComponent(id)}/query`, query),
    enabled: query !== null && !!id,
    retry: retryUnlessRefused,
    // A new filter, page or sort keeps the last answer on screen until the
    // next one arrives, instead of flashing "Loading".
    placeholderData: keepPreviousData,
  });
}

export function useDeleteBoard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, records }: { id: string; records: number }) =>
      api.delete(`/boards/${encodeURIComponent(id)}?records=${records}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: boardKeys.all }),
  });
}
