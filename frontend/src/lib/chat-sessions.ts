// Which chat a surface opens by default. `GET /chat/sessions` lists pinned
// chats first, so the list's first entry is NOT the latest conversation; the
// default is the most recently active one, whatever is pinned.

import type { ChatSessionDTO } from "@/lib/hooks-chat";

/** The session with the latest `lastMessageAt`; a session that never had a
 *  message counts as older than any that did. Ties keep list order. */
export function mostRecentSession<T extends Pick<ChatSessionDTO, "lastMessageAt">>(
  sessions: readonly T[] | undefined,
): T | undefined {
  let best: T | undefined;
  for (const s of sessions ?? []) {
    if (best === undefined) {
      best = s;
    } else if (
      s.lastMessageAt !== null &&
      (best.lastMessageAt === null || s.lastMessageAt > best.lastMessageAt)
    ) {
      best = s;
    }
  }
  return best;
}
