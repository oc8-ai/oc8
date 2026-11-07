// A member's own guardrails per agent (D3a) and the approvals those rules
// route to the member. Mirrors `backend/src/oc8/api/v1/member_guardrails.py`
// (MyGuardrailsDTO / MemberGuardrailDTO, camelCase on the wire).
//
// `level` and `baseLevel` come from the backend and are what a member row
// shows: they include the member's own approval rules, which the frontend
// `ToolPolicy` shape cannot see, so `levelOf` on the frontend would read an
// "Ask me first" rule as "Allowed".

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { useAuth, type Approval, type ToolPolicy } from "@/lib/hooks";
import type { Level } from "@/lib/guardrail-levels";

export type SummaryCode = "may_read" | "may_modify" | "asks_you" | "asks_department" | "off";

export interface SummaryLine {
  code: SummaryCode | string;
  params: { tools?: string[]; threshold?: number };
}

export interface MyToolGuardrail {
  toolKey: string;
  connectionName: string;
  /** Department ∩ agent -- what the member may only tighten. */
  base: ToolPolicy;
  baseLevel: Level;
  /** The member's own entry, verbatim; null when they set none. */
  mine: ToolPolicy | null;
  /** The level with the member's entry applied. */
  level: Level;
  hasLimits: boolean;
  source: "member" | "agent" | "department" | "capa_default" | string;
}

export interface MyGuardrails {
  agentId: string;
  agentName: string;
  summary: SummaryLine[];
  tools: MyToolGuardrail[];
}

export interface MemberGuardrail {
  memberId: string;
  memberName: string;
  tools: Record<string, ToolPolicy>;
}

export const memberGuardrailKeys = {
  mine: (agentId: string) => ["member-guardrails", agentId] as const,
  members: (agentId: string) => ["member-guardrails", agentId, "members"] as const,
  copilot: ["member-guardrails", "copilot"] as const,
};

/** The member's rules on one agent; the count of `source === "member"` rows is
 * what the chat header shows. */
export function useMyGuardrails(agentId: string) {
  return useQuery({
    queryKey: memberGuardrailKeys.mine(agentId),
    queryFn: () => api.get<MyGuardrails>(`/agents/${agentId}/guardrails/me`),
    enabled: !!agentId,
    // A 404 means "not an agent you can chat with": nothing to retry.
    retry: false,
  });
}

/** How many of the tools carry a rule of the member's own. */
export function ownRuleCount(data: MyGuardrails | undefined): number {
  return (data?.tools ?? []).filter((tool) => tool.mine !== null).length;
}

function useOnSaved(agentId: string) {
  const qc = useQueryClient();
  return (fresh: MyGuardrails) => {
    qc.setQueryData(memberGuardrailKeys.mine(agentId), fresh);
    qc.invalidateQueries({ queryKey: memberGuardrailKeys.mine(agentId) });
    qc.invalidateQueries({ queryKey: memberGuardrailKeys.copilot });
    qc.invalidateQueries({ queryKey: ["agents", agentId] });
  };
}

/** `PUT /agents/{id}/guardrails/me/{toolKey}` with exactly one of a plain
 * `level` or a `policy` (snake_case, the stored member entry). The server
 * refuses anything looser than department ∩ agent with a 422. */
export function useSetMyGuardrail(agentId: string) {
  const onSaved = useOnSaved(agentId);
  return useMutation({
    mutationFn: ({
      toolKey,
      ...body
    }: { toolKey: string } & ({ level: Level } | { policy: Record<string, unknown> })) =>
      api.put<MyGuardrails>(
        `/agents/${agentId}/guardrails/me/${encodeURIComponent(toolKey)}`,
        body,
      ),
    onSuccess: onSaved,
  });
}

/** Removes only the member's own entry for one tool. */
export function useResetMyGuardrail(agentId: string) {
  const onSaved = useOnSaved(agentId);
  return useMutation({
    mutationFn: ({ toolKey }: { toolKey: string }) =>
      api.delete<MyGuardrails>(`/agents/${agentId}/guardrails/me/${encodeURIComponent(toolKey)}`),
    onSuccess: onSaved,
  });
}

/** Everyone's own rules on an agent, for whoever manages it. Read only. */
export function useMemberGuardrails(agentId: string, enabled = true) {
  return useQuery({
    queryKey: memberGuardrailKeys.members(agentId),
    queryFn: () => api.get<MemberGuardrail[]>(`/agents/${agentId}/guardrails/members`),
    enabled: enabled && !!agentId,
  });
}

/** The `/copilot` "Guardrails" tab: every agent the member set a rule on. */
export function useCopilotGuardrails() {
  return useQuery({
    queryKey: memberGuardrailKeys.copilot,
    queryFn: () => api.get<MyGuardrails[]>("/copilot/guardrails"),
  });
}

/** A seatless member listing approvals whose filters match none of their own
 * rows gets 403 rather than `[]` (`api/v1/feed.py`) -- that is "nothing to
 * show" here, never an error page. */
async function approvalsOrNone(status: string): Promise<Approval[]> {
  try {
    return await api.get<Approval[]>(`/approvals?status=${status}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) return [];
    throw error;
  }
}

/** Pending approvals a member's own rule routed to them, across agents.
 * Under the `["approvals", ...]` prefix so a decision anywhere refreshes it. */
export function useMyApprovals({ poll = false }: { poll?: number | false } = {}) {
  const { data: me } = useAuth();
  const memberId = me?.memberId ?? null;
  return useQuery({
    queryKey: ["approvals", "mine", memberId],
    queryFn: () => approvalsOrNone("pending"),
    enabled: !!memberId,
    refetchInterval: poll,
    select: (rows) => rows.filter((row) => !!memberId && row.assigneeMemberId === memberId),
  });
}

export type NextStageStatus = "pending" | "approved" | "rejected" | "expired" | null;

/** How often to look again at a member's approved row: only while the
 * department's stage is still open. Decided, or no stage at all -> stop. */
export function stagePollInterval(status: NextStageStatus | undefined): number | false {
  return status === "pending" ? 5000 : false;
}

/** The member's own approved row, re-read for its `nextStageStatus` -- how the
 * department's second stage stands. Reads only the member's own row (the
 * `status=approved` listing shows a member their own rows), never the
 * department's. Enabled only while a department stage is known to be open;
 * polls until it is decided. */
export function useApprovalStage(approvalId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["approvals", "stage", approvalId],
    queryFn: async () =>
      (await approvalsOrNone("approved")).find((row) => row.id === approvalId) ?? null,
    enabled: enabled && !!approvalId,
    refetchInterval: (query) =>
      query.state.data === undefined
        ? 5000
        : stagePollInterval(query.state.data?.nextStageStatus ?? null),
  });
}
