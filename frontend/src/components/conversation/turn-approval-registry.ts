// frontend/src/components/conversation/turn-approval-registry.ts
// Approvals a turn's run is held on, answered inside that turn (spec §4.2,
// plan Task 43). Core shows only `request_decision` approvals there, through
// DecisionCard (conversation.tsx). Every other kind of approval card a
// feature wants in the turn registers below: a fixed, compiled list like
// RUN_COMPONENT_REGISTRY (run-record-card.tsx), never a dynamic lookup.
// Rendered by TurnApprovalCards (turn-approval-cards.tsx).
//
// An entry brings its OWN source of approvals, not core's `approval:view`
// list: a member's own guardrail (PR #71) asks a member who may hold no seat
// and so no `approval:view` at all.
import type { ComponentType } from "react";

import type { Approval } from "@/lib/hooks";

export interface TurnApprovalCardEntry {
  /** The approvals this card answers, pending ones. Called as a hook, once
   *  per turn that has a run; `poll` is set while that run is live. It must
   *  return only approvals the viewer may see -- the slot shows whatever
   *  comes back that holds the turn's run, and checks nothing itself. */
  useApprovals: (options: { poll: number | false }) => { data?: Approval[] | undefined };
  Card: ComponentType<{ approval: Approval }>;
}

/** Keyed by the card's name. PR #71 adds `member_guardrail` here. */
export const TURN_APPROVAL_CARDS: Record<string, TurnApprovalCardEntry> = {};
