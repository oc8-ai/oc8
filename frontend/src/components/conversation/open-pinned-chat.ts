// Opening a Copilot chat with an object pinned (spec §5.6). The `?pin=` value
// is untrusted, so it is parsed strictly before anything is created. Pinning
// itself is checked server-side with the member's own rights.
import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { api, ApiError } from "@/lib/api";
import type { ChatSessionDetailDTO } from "@/lib/conversation-api";
import { chatKeys, type ChatSessionDTO } from "@/lib/hooks-chat";
import { isUuid } from "@/lib/ids";

/** The kinds `PUT /chat/sessions/{id}/pins` accepts. */
const PIN_KINDS = new Set([
  "knowledge_base",
  "data_source",
  "source_file",
  "file",
  "agent",
  "department",
  "run",
  "approval",
  "responsibility",
  "chat_session",
]);
const MAX_FILE_PART = 2048;
// eslint-disable-next-line no-control-regex -- rejecting control characters is the point
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

export interface ParsedPin {
  kind: string;
  id: string;
}

/** A `?pin=` value taken as a pin, or null. The parameter is untrusted (any
 *  link can carry it): `<kind>:<uuid>`, and for `source_file`
 *  `<uuid>:<file>` with the file part URL-decoded, 1..2048 characters and no
 *  control characters. */
export function parsePin(raw: unknown): ParsedPin | null {
  if (typeof raw !== "string") return null;
  const cut = raw.indexOf(":");
  if (cut <= 0) return null;
  const kind = raw.slice(0, cut);
  const rest = raw.slice(cut + 1);
  if (!PIN_KINDS.has(kind)) return null;
  if (kind !== "source_file") return isUuid(rest) ? { kind, id: rest } : null;
  const split = rest.indexOf(":");
  if (split <= 0 || !isUuid(rest.slice(0, split))) return null;
  let file: string;
  try {
    file = decodeURIComponent(rest.slice(split + 1));
  } catch {
    return null;
  }
  if (!file || file.length > MAX_FILE_PART || CONTROL_CHARS.test(file)) return null;
  return { kind, id: `${rest.slice(0, split)}:${file}` };
}

export function discussHref(kind: string, id: string): string {
  return `/copilot?pin=${encodeURIComponent(`${kind}:${id}`)}`;
}

export interface OpenedChat {
  sessionId: string;
  /** The chat exists but the object is not pinned: "refused" when the
   *  server says this member may not pin it (422), "failed" otherwise. */
  pinned: "ok" | "refused" | "failed";
}

/** Create a Copilot chat and pin `pin` to it. Throws only when the chat could
 *  not be created; a refused or failed pin leaves the new chat in place. */
export function useOpenPinnedChat(): (pin: ParsedPin) => Promise<OpenedChat> {
  const qc = useQueryClient();
  return useCallback(
    async (pin: ParsedPin) => {
      const assistant = await qc.fetchQuery({
        queryKey: ["assistant"],
        queryFn: () => api.get<{ agentId: string }>("/assistant"),
        staleTime: Infinity,
      });
      const session = await api.post<ChatSessionDTO>("/chat/sessions", {
        agentId: assistant.agentId,
      });
      void qc.invalidateQueries({ queryKey: chatKeys.sessions(session.agentId) });
      void qc.invalidateQueries({ queryKey: chatKeys.sessions() });
      try {
        await api.put<ChatSessionDetailDTO>(`/chat/sessions/${session.id}/pins`, {
          refs: [{ kind: pin.kind, id: pin.id }],
        });
        return { sessionId: session.id, pinned: "ok" as const };
      } catch (error) {
        const refused = error instanceof ApiError && error.status === 422;
        return {
          sessionId: session.id,
          pinned: refused ? ("refused" as const) : ("failed" as const),
        };
      }
    },
    [qc],
  );
}
