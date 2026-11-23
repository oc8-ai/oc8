import type { GuardrailValue } from "@/components/guardrail-preset-picker";
import type { AgentDetail } from "@/lib/hooks-agent-detail";

// Where the agent page's autonomy setting lives: the config tab, this anchor.
export const AUTONOMY_ANCHOR = "agent-autonomy";

/** The outward tools `value` lets run with no approval asked by policy:
 * modify granted, the tool within `only`, and neither the tool, its
 * connection nor the modify right listed in `approvalActions` (the PDP's
 * three ways to ask). Such tools still ask unless the agent itself is
 * autonomous with the outward tier granted (`b_approval.posture`). */
export function unaskedOutwardTools(
  value: GuardrailValue,
  toolKey: string,
  outwardTools: string[],
): string[] {
  if (!value.modify) return [];
  const asks = new Set(value.approvalActions);
  if (asks.has("modify") || asks.has(toolKey)) return [];
  return outwardTools.filter(
    (tool) => !asks.has(tool) && (value.only.length === 0 || value.only.includes(tool)),
  );
}

/** Whether the agent sends to the outside without asking: autonomous AND the
 * outward tier granted, the same two conditions `b_approval.posture` checks. */
export function actsOutwardUnasked(agent: Pick<AgentDetail, "autonomy" | "autonomyGrants">) {
  return agent.autonomy === "autonomous" && (agent.autonomyGrants ?? []).includes("outward");
}
