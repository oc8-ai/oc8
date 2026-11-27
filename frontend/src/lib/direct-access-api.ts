// The chat's own door to the Copilot's direct access: the conversation's
// member sees their own approvals, decides them and revokes grants.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { Approval } from "@/lib/hooks";

export interface DirectAccessGrant {
  id: string;
  sessionId: string;
  toolKey: string;
  label: string;
  approvalId: string;
  grantedAt: string;
}

const ROOT = ["direct-access"] as const;

/** The caller's own direct-access approvals: a TURN_APPROVAL_CARDS source. */
export function useMyDirectAccessApprovals({
  poll,
  sessionId,
}: {
  poll: number | false;
  /** Narrows the list to this chat, so a chosen-agent note stays in its own. */
  sessionId?: string | null;
}) {
  return useQuery({
    queryKey: [...ROOT, "approvals", sessionId ?? ""],
    queryFn: () =>
      api.get<Approval[]>(
        sessionId
          ? `/chat/direct-access/approvals?sessionId=${encodeURIComponent(sessionId)}`
          : "/chat/direct-access/approvals",
      ),
    refetchInterval: poll,
  });
}

export function useDirectAccessGrants(sessionId: string | null) {
  return useQuery({
    queryKey: [...ROOT, "grants", sessionId ?? ""],
    queryFn: () =>
      api.get<DirectAccessGrant[]>(
        `/chat/sessions/${encodeURIComponent(sessionId ?? "")}/direct-access`,
      ),
    enabled: !!sessionId,
  });
}

export function useDecideDirectAccess() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      approvalId,
      decision,
      reason,
      agentId,
      alwaysAllow,
    }: {
      approvalId: string;
      decision: "approve" | "reject";
      reason?: string;
      /** Approve by running this offered agent instead of granting direct access. */
      agentId?: string;
      /** Also let this tool run for the person without asking again. */
      alwaysAllow?: boolean;
    }) =>
      api.post<Approval>(
        `/chat/direct-access/approvals/${encodeURIComponent(approvalId)}/decision`,
        {
          decision,
          reason: reason ?? "",
          ...(agentId ? { agentId } : {}),
          ...(alwaysAllow ? { alwaysAllow: true } : {}),
        },
      ),
    onSuccess: (_dto, vars) => {
      void qc.invalidateQueries({ queryKey: ROOT });
      if (vars.alwaysAllow) void qc.invalidateQueries({ queryKey: ["tool-allow-rules"] });
    },
  });
}

export function useRevokeDirectAccess(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (grantId: string) =>
      api.post<void>(
        `/chat/sessions/${encodeURIComponent(sessionId)}/direct-access/${encodeURIComponent(grantId)}/revoke`,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ROOT }),
  });
}
