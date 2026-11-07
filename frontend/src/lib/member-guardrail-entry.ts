import type { Condition, GuardrailValue } from "@/components/guardrail-preset-picker";
import type { ToolPolicy } from "@/lib/hooks";
import type { MyToolGuardrail } from "@/lib/hooks-member-guardrails";

// The member's own entry in "More" (D3a): what the editor starts from and
// what a save stores. Kept out of the sheet so it can be tested on its own.

export function toValue(policy: ToolPolicy): GuardrailValue {
  return {
    read: policy.read,
    modify: policy.modify,
    approvalActions: policy.approvalActions ?? [],
    approvalEur: policy.approvalEur ?? null,
    only: policy.only ?? [],
    conditions: policy.conditions ?? [],
  };
}

/** What "More" starts from. The member's own entry when they have one;
 * otherwise only the rights the department and agent grant -- never their
 * approvals, threshold or conditions, which already apply on their own and
 * would otherwise be copied into the member's term. */
export function seedValue(tool: Pick<MyToolGuardrail, "base" | "mine">): GuardrailValue {
  if (tool.mine) return toValue(tool.mine);
  return {
    read: tool.base.read,
    modify: tool.base.modify,
    approvalActions: [],
    approvalEur: null,
    only: [],
    conditions: [],
  };
}

function sameCondition(a: Condition, b: Condition): boolean {
  return (
    a.attribute === b.attribute &&
    a.datatype === b.datatype &&
    a.operator === b.operator &&
    a.then === b.then &&
    JSON.stringify(a.value) === JSON.stringify(b.value)
  );
}

/** A "More" edit as the stored member entry (snake_case, like the agent's
 * narrowing save). The server refuses anything looser than department ∩ agent.
 * Parts the base already implies are dropped -- an approval action the base
 * asks for, a condition it already has, a threshold at or above its own -- so
 * the member's entry only holds what is theirs (backend `_adds_nothing`). */
export function toEntry(value: GuardrailValue, base: ToolPolicy): Record<string, unknown> {
  const baseActions = new Set(base.approvalActions ?? []);
  const baseConditions = base.conditions ?? [];
  const eur = value.approvalEur ?? null;
  const implied = eur !== null && base.approvalEur !== null && base.approvalEur <= eur;
  return {
    enabled: value.read || value.modify,
    read: value.read,
    modify: value.modify,
    approval_actions: value.approvalActions.filter((a) => !baseActions.has(a)),
    approval_eur: implied ? null : eur,
    ...(value.only && value.only.length > 0 ? { only: value.only } : {}),
    conditions: (value.conditions ?? []).filter(
      (c) => !baseConditions.some((b) => sameCondition(b, c)),
    ),
  };
}
