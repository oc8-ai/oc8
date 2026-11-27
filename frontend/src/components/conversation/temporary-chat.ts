// A temporary chat (spec §5.6) is deleted when the person leaves it: they
// open another chat, start a new one, close the dock or go to another page.
// A closed browser tab leaves it to the server, which deletes a temporary
// chat 24 hours after its last message (backend `oc8.chat.temporary`).
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "@/lib/api";
import { forgetDeletedSession } from "@/lib/hooks-chat";

// Mounted views of each temporary chat (the dock and the Copilot page can
// show the same one), and the deletes waiting for the current render to end.
const holders = new Map<string, number>();
const pending = new Map<string, ReturnType<typeof setTimeout>>();

/** Delete `sessionId` once no view holds it any more, when `temporary`.
 *  The delete waits for the end of the current task, so a view that
 *  mounts the same chat again at once (StrictMode, a layout switch) keeps
 *  it. `onLeft` hears of each chat deleted this way, also after this view
 *  unmounted, so its owner can forget the id it kept. */
export function useLeaveTemporaryChat(
  sessionId: string | null,
  temporary: boolean,
  onLeft?: (sessionId: string) => void,
) {
  const qc = useQueryClient();
  const onLeftRef = useRef(onLeft);
  onLeftRef.current = onLeft;
  useEffect(() => {
    if (!sessionId || !temporary) return;
    holders.set(sessionId, (holders.get(sessionId) ?? 0) + 1);
    const waiting = pending.get(sessionId);
    if (waiting !== undefined) {
      clearTimeout(waiting);
      pending.delete(sessionId);
    }
    return () => {
      const left = (holders.get(sessionId) ?? 1) - 1;
      if (left > 0) {
        holders.set(sessionId, left);
        return;
      }
      holders.delete(sessionId);
      const notify = onLeftRef.current;
      pending.set(
        sessionId,
        setTimeout(() => {
          pending.delete(sessionId);
          // Best effort: a delete that fails leaves the chat to the purge.
          api.delete<void>(`/chat/sessions/${encodeURIComponent(sessionId)}`).then(
            () => forgetDeletedSession(qc, sessionId),
            () => undefined,
          );
          notify?.(sessionId);
        }, 0),
      );
    };
  }, [sessionId, temporary, qc]);
}
