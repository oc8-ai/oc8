// frontend/src/components/conversation/turn-approval-cards.tsx
// The cards registered in TURN_APPROVAL_CARDS (turn-approval-registry.ts) for
// the approvals one turn's run is held on.
import { useEffect, useState } from "react";

import type { Approval } from "@/lib/hooks";

import { TURN_APPROVAL_CARDS, type TurnApprovalCardEntry } from "./turn-approval-registry";

/** How often an entry looks again while the turn's run is live. */
const LIVE_POLL_MS = 3000;

/** Every registered card for the approvals held on `runId`. Nothing for a
 *  turn without a run. */
export function TurnApprovalCards({ runId, live }: { runId: string | null; live: boolean }) {
  if (!runId) return null;
  return (
    <>
      {Object.entries(TURN_APPROVAL_CARDS).map(([key, entry]) => (
        <EntryCards key={key} entry={entry} runId={runId} live={live} />
      ))}
    </>
  );
}

function EntryCards({
  entry,
  runId,
  live,
}: {
  entry: TurnApprovalCardEntry;
  runId: string;
  live: boolean;
}) {
  const { data } = entry.useApprovals({ poll: live ? LIVE_POLL_MS : false });
  // Kept once seen: a decided approval leaves the pending list, and its card
  // stays to say what happened next. Decisions are DecisionCard's.
  const [shown, setShown] = useState<Approval[]>([]);
  useEffect(() => {
    const held = (data ?? []).filter((a) => a.runId === runId && a.actionType !== "decision");
    if (held.some((a) => !shown.some((s) => s.id === a.id))) {
      setShown((prev) => [...prev, ...held.filter((a) => !prev.some((p) => p.id === a.id))]);
    }
  }, [data, runId, shown]);
  const { Card } = entry;
  return (
    <>
      {shown.map((approval) => (
        <Card key={approval.id} approval={approval} />
      ))}
    </>
  );
}
