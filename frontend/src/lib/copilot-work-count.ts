import { useApprovals, useClarifications } from "@/lib/hooks";
import { useCopilotDelegations, useFollowups, useResponsibilities } from "@/lib/hooks-copilot";

const ACTIVE_RUN_STATES = new Set([
  "queued",
  "running",
  "waiting_for_input",
  "waiting_for_approval",
]);

/** How many things the Copilot page's work rail has to show: open
 *  responsibilities, delegated runs still going, scheduled follow-ups, and
 *  approvals/questions the Copilot is waiting on. Notes are left out on
 *  purpose -- they are standing memory, not work, and would keep the rail
 *  open for good. Zero means the rail can stay folded away.
 *
 *  Same query keys as the panel itself, so this costs no extra request. */
export function useCopilotWorkCount(agentId: string | null | undefined): number {
  const { data: responsibilities = [] } = useResponsibilities();
  const { data: runs = [] } = useCopilotDelegations();
  const { data: followups = [] } = useFollowups();
  const { data: approvals = [] } = useApprovals("pending");
  const { data: clarifications = [] } = useClarifications();
  const open = responsibilities.filter((r) => r.state !== "done" && r.state !== "cancelled");
  const active = runs.filter((r) => ACTIVE_RUN_STATES.has(r.state));
  const scheduled = followups.filter((f) => f.enabled);
  const waiting = agentId
    ? approvals.filter((a) => a.agentId === agentId).length +
      clarifications.filter((c) => c.agentId === agentId).length
    : 0;
  return open.length + active.length + scheduled.length + waiting;
}
