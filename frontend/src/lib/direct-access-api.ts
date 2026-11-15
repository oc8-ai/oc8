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
export function useMyDirectAccessApprovals({ poll }: { poll: number | false }) {
  return useQuery({
    queryKey: [...ROOT, "approvals"],
    queryFn: () => api.get<Approval[]>("/chat/direct-access/approvals"),
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
    }: {
      approvalId: string;
      decision: "approve" | "reject";
      reason?: string;
    }) =>
      api.post<Approval>(
        `/chat/direct-access/approvals/${encodeURIComponent(approvalId)}/decision`,
        { decision, reason: reason ?? "" },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ROOT }),
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
