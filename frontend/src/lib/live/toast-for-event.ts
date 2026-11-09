import { toast } from "sonner";

import type { RealtimeEvent } from "@/lib/live/types";

const COOLDOWN_MS = 4000;
const lastShown: Record<string, number> = {};

// Id of the agent currently open on its detail page (Live Log tab). Events
// about that agent are already visible live, so we suppress the toast for
// them — set/cleared by the agent detail route on mount/unmount.
let viewedAgentId: string | null = null;

export function setViewedAgent(id: string | null): void {
  viewedAgentId = id;
}

// Event types that carry an `agent_id` field on their payload (matches the
// patchers in apply-event.ts, which read `d.agent_id` the same way).
const AGENT_SCOPED_TYPES = new Set(["agent.status", "run.status", "activity.logged"]);

function cooled(key: string): boolean {
  const now = Date.now();
  if (now - (lastShown[key] ?? 0) < COOLDOWN_MS) return false;
  lastShown[key] = now;
  return true;
}

type Translate = (en: string, de?: string) => string;
const english: Translate = (en) => en;

/** A run some chat is answering with: its answer streams in that chat (the
 *  reader's own, or the one they have open), so it is no background work. */
function isChatRun(e: RealtimeEvent): boolean {
  return e.data.source === "chat";
}

export function toastForEvent(e: RealtimeEvent, t: Translate = english): void {
  if (AGENT_SCOPED_TYPES.has(e.type)) {
    const agentId = e.data.agent_id as string | undefined;
    if (agentId && agentId === viewedAgentId) return; // already watching this agent live
  }

  if (e.type === "run.status") {
    // Only terminal states are toast-worthy (matches RunState.TERMINAL in
    // backend/src/oc8/runtime/states.py and the invalidation check in
    // apply-event.ts). Non-terminal transitions (queued/running/waiting_*)
    // stay silent — the viewed-agent guard above already handles the case
    // where you're watching this run live.
    const state = e.data.state as string | undefined;
    if (state !== "done" && state !== "failed" && state !== "interrupted") return;
    if (isChatRun(e)) return;
    if (!cooled(e.type)) return;
    if (state === "done") {
      toast.success(t("Agent run finished", "Agent-Run abgeschlossen"), {
        description: t(
          "A background agent finished its run.",
          "Ein Hintergrund-Agent hat seinen Lauf beendet.",
        ),
      });
    } else if (state === "failed") {
      toast.error(t("Agent run failed", "Agent-Run fehlgeschlagen"), {
        description: t(
          "A background agent's run failed.",
          "Ein Hintergrund-Agent-Lauf ist fehlgeschlagen.",
        ),
      });
    } else {
      toast.warning(t("Agent run interrupted", "Agent-Run unterbrochen"), {
        description: t(
          "A background agent's run was interrupted.",
          "Ein Hintergrund-Agent-Lauf wurde unterbrochen.",
        ),
      });
    }
    return;
  }

  if (e.type === "approval.created") {
    if (!cooled(e.type)) return;
    toast.info(t("New approval requested", "Neue Freigabe angefordert"), {
      description: t(
        "An agent is waiting for your decision.",
        "Ein Agent wartet auf deine Entscheidung.",
      ),
    });
    return;
  }

  if (e.type === "approval.decided") {
    if (!cooled(e.type)) return;
    const status = e.data.status as string | undefined;
    toast(
      status === "approved"
        ? t("Approval granted", "Freigabe erteilt")
        : t("Approval rejected", "Freigabe abgelehnt"),
      { description: t("A decision was made.", "Eine Entscheidung wurde getroffen.") },
    );
    return;
  }

  if (e.type === "handoff.status") {
    if (!cooled(e.type)) return;
    toast(t("Handoff updated", "Übergabe aktualisiert"), {
      description: t(
        "The status of an agent handoff changed.",
        "Der Status einer Agenten-Übergabe hat sich geändert.",
      ),
    });
    return;
  }
}
