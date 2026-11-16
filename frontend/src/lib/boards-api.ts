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
  /** The person may start the producing agent, so may ask it to refresh. */
  mayRun: boolean;
  /** The person may share and unshare: the same rule as `mayDelete`. */
  mayShare: boolean;
  /** Visible to the person only through a share, not through their own reach. */
  sharedWithMe: boolean;
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
  shares: (id: string) => ["boardShares", id] as const,
  targets: (id: string, q: string) => ["boardShareTargets", id, q] as const,
  /** Set once this client deleted the board. Outside `["boards", ...]` on
   *  purpose, so removing or invalidating the board's queries leaves it. */
  deleted: (id: string) => ["boardDeleted", id] as const,
};

/** Whether this client deleted the board. A tile that is still mounted reads
 *  it to show "removed" and to stop its board queries: a removed query is not
 *  told it was removed, so the next render would rebuild and refetch it. */
export function useBoardDeleted(id: string): boolean {
  const { data } = useQuery({
    queryKey: boardKeys.deleted(id),
    queryFn: () => false,
    // Never fetched: only `useDeleteBoard` writes it.
    enabled: false,
    staleTime: Infinity,
  });
  return data === true;
}

export function useBoards(agentId?: string) {
  return useQuery({
    queryKey: boardKeys.list(agentId),
    queryFn: () =>
      api.get<BoardSummary[]>(
        agentId ? `/boards?agentId=${encodeURIComponent(agentId)}` : "/boards",
      ),
    // A list key (`["boards", agentId]`) has the same shape as a board's
    // (`["boards", id]`); this tag is how a delete finds the lists.
    meta: { boardList: true },
  });
}

export function useBoard(id: string) {
  const deleted = useBoardDeleted(id);
  return useQuery({
    queryKey: boardKeys.one(id),
    queryFn: () => api.get<Board>(`/boards/${encodeURIComponent(id)}`),
    enabled: !!id && !deleted,
    retry: retryUnlessRefused,
  });
}

export function useBoardQuery(id: string, query: BoardQuery | null) {
  const deleted = useBoardDeleted(id);
  return useQuery({
    queryKey: boardKeys.query(id, query),
    queryFn: () => api.post<QueryResult>(`/boards/${encodeURIComponent(id)}/query`, query),
    enabled: query !== null && !!id && !deleted,
    retry: retryUnlessRefused,
    // A new filter, page or sort keeps the last answer on screen until the
    // next one arrives, instead of flashing "Loading".
    placeholderData: keepPreviousData,
  });
}

/** The delete was refused because records arrived since the count the
 *  person confirmed (`409 board_records_changed`). */
export function recordsChanged(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error as Error & { status?: unknown }).status === 409 &&
    error.message === "board_records_changed"
  );
}

export function useDeleteBoard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, records }: { id: string; records: number }) =>
      api.delete(`/boards/${encodeURIComponent(id)}?records=${records}`),
    onSuccess: async (_data, { id }) => {
      // Mark it deleted first: that re-renders a still-mounted tile as
      // "removed" with its queries disabled, so no later render refetches it.
      qc.setQueryData(boardKeys.deleted(id), true);
      // Then drop the board and its queries: marking them stale would make a
      // still-mounted tile refetch and flash a 404/410. An answer still in
      // flight is cancelled, or its arrival would bring the board back.
      await qc.cancelQueries({ queryKey: boardKeys.one(id) });
      qc.removeQueries({ queryKey: boardKeys.one(id) });
      return qc.invalidateQueries({ predicate: (q) => q.meta?.boardList === true });
    },
    onError: (error, { id }) => {
      if (!recordsChanged(error)) return;
      // The board is still there with more records: read its count again, in
      // the board and in the lists, so the next confirmation names it.
      return Promise.all([
        qc.invalidateQueries({ queryKey: boardKeys.one(id), exact: true }),
        qc.invalidateQueries({ predicate: (q) => q.meta?.boardList === true }),
      ]);
    },
  });
}

/** `BoardShareDTO`: exactly one of member and department is set. */
export interface BoardShare {
  id: string;
  memberId: string | null;
  memberName: string | null;
  departmentId: string | null;
  departmentName: string | null;
  createdAt: string;
}

export interface ShareTarget {
  id: string;
  name: string;
}

/** `ShareTargetsDTO`: who the board can be shared with (at most 20 of each). */
export interface ShareTargets {
  members: ShareTarget[];
  departments: ShareTarget[];
}

export function useBoardShares(id: string, enabled = true) {
  return useQuery({
    queryKey: boardKeys.shares(id),
    queryFn: () => api.get<BoardShare[]>(`/boards/${encodeURIComponent(id)}/shares`),
    enabled: !!id && enabled,
    retry: retryUnlessRefused,
  });
}

export function useShareTargets(id: string, q: string, enabled = true) {
  return useQuery({
    queryKey: boardKeys.targets(id, q),
    queryFn: () =>
      api.get<ShareTargets>(
        `/boards/${encodeURIComponent(id)}/share-targets?q=${encodeURIComponent(q)}`,
      ),
    enabled: !!id && enabled,
    retry: retryUnlessRefused,
    placeholderData: keepPreviousData,
  });
}

function useInvalidateShares() {
  const qc = useQueryClient();
  return (id: string) =>
    Promise.all([
      qc.invalidateQueries({ queryKey: boardKeys.shares(id) }),
      qc.invalidateQueries({ predicate: (q) => q.meta?.boardList === true }),
    ]);
}

export function useShareBoard() {
  const invalidate = useInvalidateShares();
  return useMutation({
    mutationFn: ({
      id,
      ...target
    }: { id: string } & ({ memberId: string } | { departmentId: string })) =>
      api.post<BoardShare>(`/boards/${encodeURIComponent(id)}/shares`, target),
    onSuccess: (_data, { id }) => invalidate(id),
  });
}

export function useUnshareBoard() {
  const invalidate = useInvalidateShares();
  return useMutation({
    mutationFn: ({ id, shareId }: { id: string; shareId: string }) =>
      api.delete(`/boards/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}`),
    onSuccess: (_data, { id }) => invalidate(id),
  });
}
