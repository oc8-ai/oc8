// Hooks for the auditor screen (§ audit hash-chain, reading side). Kept in a
// separate module from `hooks.ts` (not edited here), matching the pattern in
// knowledge-connector-hooks.ts.

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, API_URL, getToken } from "./api";

export interface AuditEvent {
  id: string;
  seq: number;
  ts: string;
  actorType: string;
  actorId: string | null;
  category: string;
  action: string;
  resource: Record<string, unknown>;
  decision: string | null;
  reason: string | null;
  responsibleType: string | null;
  responsibleId: string | null;
  hash: string;
  prevHash: string;
}

export interface AuditPage {
  events: AuditEvent[];
  nextBeforeSeq: number | null;
}

export interface AuditIntegrity {
  /** `unverifiable` is NOT a verdict on the chain: it means the verifier could
   * not run at all (the MAC key is missing, rotated or malformed). It is not
   * sticky — a later run with the key present returns the tenant to "ok". */
  status: "ok" | "broken" | "unverifiable" | "never";
  verifiedThroughSeq: number;
  headHash: string | null;
  verifiedAt: string | null;
  brokenAtSeq: number | null;
  /** Why the chain is broken. `hash_mismatch`: a row no longer hashes
   * correctly — a full check is the remedy path after a legitimate restore.
   * `truncation`: entries are gone — no re-check can clear it, only the rows
   * coming back from a backup. `mac_downgrade`: the checkpoint's tamper-proof
   * seal is missing or no longer authenticates — needs the seal re-issued out
   * of band (`oc8 audit-adopt-checkpoints`) before a full check can clear it.
   * `unknown_mac_version`: a row carries a MAC version no legitimate writer
   * produces. Null unless status is "broken". */
  breakKind: "hash_mismatch" | "truncation" | "mac_downgrade" | "unknown_mac_version" | null;
  /** How many previously-present entries are gone, exactly -- the server
   * knows this as a COUNT (verified_count minus what's still present), not a
   * seq range: for a deletion in the middle of the chain, "entries after seq
   * N are missing" would be false even though N entries are gone somewhere
   * before the tail. Null unless breakKind is "truncation". */
  missingCount: number | null;
  /** When a break was FIRST observed. Write-once server-side: it survives a
   * later successful full verification, so it can be non-null in the "ok"
   * state too. */
  firstBreakAt: string | null;
  eventCount: number;
}

export interface AuditFilters {
  category?: string;
  action?: string;
  actorType?: string;
  decision?: string;
  from?: string;
  to?: string;
  /** The run (and chat) a tool call came from: `resource.run_id` /
   *  `resource.chat_session_id`. Only a well-formed UUID; the server answers
   *  anything else with 422. */
  runId?: string;
  chatSessionId?: string;
}

const PARAM: Partial<Record<keyof AuditFilters, string>> = {
  actorType: "actor_type",
  runId: "run_id",
  chatSessionId: "chat_session_id",
};

function toParams(filters: AuditFilters): URLSearchParams {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v) p.set(PARAM[k as keyof AuditFilters] ?? k, v);
  }
  return p;
}

export function useAuditEvents(filters: AuditFilters) {
  return useInfiniteQuery({
    queryKey: ["audit", "events", filters],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const p = toParams(filters);
      p.set("limit", "50");
      if (pageParam) p.set("before_seq", String(pageParam));
      return api.get<AuditPage>(`/audit?${p.toString()}`);
    },
    getNextPageParam: (last: AuditPage) => last.nextBeforeSeq,
  });
}

export function useAuditIntegrity() {
  return useQuery({
    queryKey: ["audit", "integrity"],
    queryFn: () => api.get<AuditIntegrity>("/audit/integrity"),
  });
}

export function useVerifyChain() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (full: boolean) => api.post<AuditIntegrity>(`/audit/verify?full=${full}`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["audit", "integrity"] }),
  });
}

export async function downloadAuditExport(
  filters: AuditFilters,
  format: "csv" | "jsonl",
): Promise<void> {
  const p = toParams(filters);
  p.set("format", format);
  const res = await fetch(`${API_URL}/audit/export?${p.toString()}`, {
    headers: { authorization: `Bearer ${await getToken()}` },
  });
  if (!res.ok) throw new Error(`export failed: ${res.status}`);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = `audit-export.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}
