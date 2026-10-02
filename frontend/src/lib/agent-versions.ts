// TanStack Query hooks for an agent's published configuration versions.
//
// Its own module rather than an addition to hooks.ts (2887 lines) or
// hooks-agent-detail.ts, matching audit-hooks.ts / governance-hooks.ts /
// roles-hooks.ts. It reuses the ["agents", id] key that hooks-agent-detail.ts
// and the live event patcher (live/apply-event.ts) already target, so a publish
// refreshes the agent header without touching either of those files.
//
// Mirrors lib/skills.ts's thin shape: an id plus a number, and no client-side
// version state machine. Whether the working copy has drifted is answered by
// the SERVER (GET /agents/{id}/draft-status), never recomputed here -- the
// backend compares payload hashes and the publish endpoint refuses a no-op with
// a 409, so a second, client-side idea of "dirty" would eventually disagree
// with the API and show a Publish button that 409s.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Page } from "@/lib/hooks";

/** Mirrors backend `AgentVersionSummaryDTO`. No `payload`: the list would
 *  otherwise ship a full configuration per row to render a table. */
export interface AgentVersionSummary {
  id: string;
  versionNo: number;
  note: string | null;
  /** `org_member.id`, or null for the migration's backfilled v1 and for any
   *  version published by something other than a person. */
  publishedBy: string | null;
  publishedAt: string;
  isCurrent: boolean;
  /** Set when this version came from a rollback, naming the version it copied. */
  rolledBackFrom: number | null;
}

/** Mirrors backend `AgentVersionDTO`. */
export interface AgentVersion extends AgentVersionSummary {
  payload: Record<string, unknown>;
  /** Hex. JSON has no bytes type and hex is what an operator can compare
   *  against `digest()` output in psql. */
  payloadHash: string;
}

export interface AgentVersionFieldDiff {
  /** Either a payload key (`mission`) or one level inside a JSONB one
   *  (`narrowing.odoo`, `definition.max_steps`). */
  field: string;
  before: unknown;
  after: unknown;
}

export interface AgentVersionDiff {
  fromVersionNo: number;
  /** Null means the right-hand side is the unpublished working copy, not a
   *  published version. */
  toVersionNo: number | null;
  entries: AgentVersionFieldDiff[];
}

export interface AgentDraftStatus {
  dirty: boolean;
  /** Granular: two threshold edits on two connections are two entries, not one
   *  "narrowing". This is what "N unpublished changes" counts. */
  changedFields: string[];
  currentVersionNo: number | null;
}

export const agentVersionKeys = {
  list: (agentId: string) => ["agents", agentId, "versions"] as const,
  one: (agentId: string, versionNo: number) => ["agents", agentId, "versions", versionNo] as const,
  diff: (agentId: string, from: number, to: number | null) =>
    ["agents", agentId, "versions", "diff", from, to] as const,
  draftStatus: (agentId: string) => ["agents", agentId, "draft-status"] as const,
};

/** Newest first. `limit` defaults to 50: the Versions tab is a scrollable list,
 *  not a paged grid, and 50 versions of one agent is already an unusual amount
 *  of churn. The backend caps it at 100. */
export function useAgentVersions(agentId: string, opts?: { limit?: number }) {
  const limit = opts?.limit ?? 50;
  return useQuery({
    queryKey: [...agentVersionKeys.list(agentId), limit],
    queryFn: () =>
      api.get<Page<AgentVersionSummary>>(`/agents/${agentId}/versions?limit=${limit}&offset=0`),
    enabled: !!agentId,
  });
}

export function useAgentVersion(agentId: string, versionNo: number | null) {
  return useQuery({
    queryKey: agentVersionKeys.one(agentId, versionNo ?? -1),
    queryFn: () => api.get<AgentVersion>(`/agents/${agentId}/versions/${versionNo}`),
    enabled: !!agentId && versionNo !== null,
  });
}

/** `to` omitted (or null) diffs against the WORKING COPY -- what the publish
 *  bar's Review button asks for. The parameter is left off the query string
 *  entirely in that case: `to=null` arrives at FastAPI as the four-character
 *  string "null" and 422s on the `int` converter. */
export function useAgentVersionDiff(agentId: string, from: number | null, to?: number | null) {
  const target = to ?? null;
  return useQuery({
    queryKey: agentVersionKeys.diff(agentId, from ?? -1, target),
    queryFn: () =>
      api.get<AgentVersionDiff>(
        `/agents/${agentId}/versions/diff?from=${from}` + (target !== null ? `&to=${target}` : ""),
      ),
    enabled: !!agentId && from !== null,
  });
}

/** The publish bar's data source. Its own key rather than a field on the agent
 *  query: thirteen write endpoints move it and a publish clears it, and
 *  invalidating it must not drag the agent's whole effective-tools computation
 *  along. */
export function useAgentDraftStatus(agentId: string) {
  return useQuery({
    queryKey: agentVersionKeys.draftStatus(agentId),
    queryFn: () => api.get<AgentDraftStatus>(`/agents/${agentId}/draft-status`),
    enabled: !!agentId,
  });
}

export interface PublishAgentVersionInput {
  note?: string;
  /** What the client believed was current. Required by the API, and null only
   *  for an agent that has never been published -- a mismatch is a 409, which is
   *  the whole of the optimistic check. */
  expectedCurrentVersionNo: number | null;
}

function invalidateAfterPublish(qc: ReturnType<typeof useQueryClient>, agentId: string) {
  // Three keys, and each for its own reason:
  //   * the version list gained a row;
  //   * the draft status is now clean;
  //   * the AGENT itself carries `currentVersionNo`, which the header renders.
  // The first is a prefix invalidation so every `limit` variant refetches.
  qc.invalidateQueries({ queryKey: agentVersionKeys.list(agentId) });
  qc.invalidateQueries({ queryKey: agentVersionKeys.draftStatus(agentId) });
  qc.invalidateQueries({ queryKey: ["agents", agentId] });
}

export function usePublishAgentVersion(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: PublishAgentVersionInput) =>
      api.post<AgentVersion>(`/agents/${agentId}/versions`, input),
    onSuccess: () => invalidateAfterPublish(qc, agentId),
  });
}

/** Takes the version number to roll back TO. No body: the target is in the path
 *  and the note is generated server-side (`"Rollback to vN"`).
 *
 *  A rollback also rewrites the agent ROW (it is a publish of an old payload,
 *  not a repoint), so it invalidates exactly what a publish does. */
export function useRollbackAgentVersion(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (versionNo: number) =>
      api.post<AgentVersion>(`/agents/${agentId}/versions/${versionNo}/rollback`),
    onSuccess: () => invalidateAfterPublish(qc, agentId),
  });
}
