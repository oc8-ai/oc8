import type { GuardrailValue } from "@/components/guardrail-preset-picker";

/** One way a policy goes beyond its department frame. */
export type FrameConflict =
  | { kind: "read" }
  | { kind: "modify" }
  | { kind: "approval"; names: string[] }
  | { kind: "threshold"; frame: number }
  | { kind: "only"; names: string[] };

/** Where `policy` is looser than the department `frame`: what the server
 *  (`narrowing_within_frame`) refuses when it is saved. Usage limits are left
 *  out: the department's stay in force whatever the agent sets. */
export function frameConflicts(
  policy: GuardrailValue,
  frame: GuardrailValue | null,
): FrameConflict[] {
  if (!frame) return [];
  const conflicts: FrameConflict[] = [];
  if (policy.read && !frame.read) conflicts.push({ kind: "read" });
  if (policy.modify && !frame.modify) conflicts.push({ kind: "modify" });
  const asks = new Set(policy.approvalActions);
  const dropped = asks.has("modify") ? [] : frame.approvalActions.filter((name) => !asks.has(name));
  if (dropped.length > 0) conflicts.push({ kind: "approval", names: dropped });
  if (
    frame.approvalEur != null &&
    (policy.approvalEur == null || policy.approvalEur > frame.approvalEur)
  ) {
    conflicts.push({ kind: "threshold", frame: frame.approvalEur });
  }
  if (frame.only.length > 0) {
    const allowed = new Set(frame.only);
    const beyond = policy.only.length === 0 ? [] : policy.only.filter((name) => !allowed.has(name));
    if (policy.only.length === 0 || beyond.length > 0) {
      conflicts.push({ kind: "only", names: beyond });
    }
  }
  return conflicts;
}
