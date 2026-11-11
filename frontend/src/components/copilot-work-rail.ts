import { useCallback, useMemo, useState } from "react";
import { useAuth } from "@/lib/hooks";

/** What a member last did with the Copilot page's work rail. Absent: no
 *  choice yet, so the rail follows its content. */
export type WorkRailChoice = "open" | "closed";

export function workRailStorageKey(memberId: string): string {
  return `oc8-copilot-work-rail-${memberId}`;
}

function readChoice(memberId: string): WorkRailChoice | null {
  try {
    if (typeof window === "undefined") return null;
    const raw = window.localStorage.getItem(workRailStorageKey(memberId));
    return raw === "open" || raw === "closed" ? raw : null;
  } catch {
    return null; // private window, blocked storage: just follow the content
  }
}

function writeChoice(memberId: string, choice: WorkRailChoice): void {
  try {
    window.localStorage.setItem(workRailStorageKey(memberId), choice);
  } catch {
    /* the choice still holds for this visit */
  }
}

/**
 * Whether the work rail (Activity / Scheduled / Waiting / Notes) is open.
 *
 * Folded away while there is nothing in it, so an empty column doesn't take
 * a quarter of the screen. It opens by itself the first time something
 * shows up in this visit, and then stays open even when the list empties
 * again -- a rail that vanishes under the cursor is worse than an empty one.
 * Once the member opens or closes it by hand, that choice wins, for good
 * (per member, in localStorage): new work never reopens a rail they closed.
 */
export function useWorkRail(count: number): { open: boolean; toggle: () => void } {
  const { data: me, isError } = useAuth();
  const memberId = me !== undefined ? (me.memberId ?? "anon") : isError ? "anon" : undefined;
  const stored = useMemo(() => (memberId ? readChoice(memberId) : null), [memberId]);
  const [picked, setPicked] = useState<{ memberId: string | undefined; choice: WorkRailChoice }>();
  const choice = picked && picked.memberId === memberId ? picked.choice : stored;

  // Sticky for this visit: set during render (derived state), not in an
  // effect, so the rail never paints closed for a frame once work is known.
  const [hadWork, setHadWork] = useState(false);
  if (count > 0 && !hadWork) setHadWork(true);

  const open = choice ? choice === "open" : count > 0 || hadWork;
  const toggle = useCallback(() => {
    const next: WorkRailChoice = open ? "closed" : "open";
    setPicked({ memberId, choice: next });
    if (memberId) writeChoice(memberId, next);
  }, [open, memberId]);
  return { open, toggle };
}
