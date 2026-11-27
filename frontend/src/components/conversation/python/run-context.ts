// What a Run button in an answer needs from the conversation: the chat
// uploads it may mount, whether an answer holds restricted content, and the
// conversation's own send path for "Send result to chat". Without it (user
// turns outside a conversation, the activity log) no Run is offered.

import { createContext, useContext, useMemo } from "react";

import type { FileAttachmentDTO } from "@/lib/hooks-chat";

import type { PreviewMode } from "../artifacts/context";
import type { TurnModel } from "../types";
import { LIMITS } from "./output";

export type PythonScope = {
  /** Chat uploads on the current message path, small enough to mount. */
  files: FileAttachmentDTO[];
  /** Whether a saved answer holds restricted content; undefined: not saved
   *  (a live answer, or one no longer in the chat). */
  restrictedOf: (messageId: string | undefined) => boolean | undefined;
  /** Posts a user turn through the conversation's send path; null where the
   *  chat cannot send (read-only). */
  send: ((text: string) => void) | null;
};

export const PythonRunContext = createContext<PythonScope | null>(null);

export function usePythonScope(): PythonScope | null {
  return useContext(PythonRunContext);
}

/** The scope for a conversation showing `turns` (its current path). */
export function usePythonScopeFor(
  turns: TurnModel[],
  send: ((text: string) => void) | null,
): PythonScope {
  return useMemo(() => {
    const seen = new Set<string>();
    const files: FileAttachmentDTO[] = [];
    const restricted = new Map<string, boolean>();
    for (const turn of turns) {
      if (turn.kind === "user") {
        for (const file of turn.message.attachments ?? []) {
          if (seen.has(file.id) || file.sizeBytes > LIMITS.fileBytes) continue;
          seen.add(file.id);
          files.push(file);
        }
      } else {
        for (const v of turn.variants) restricted.set(v.id, v.restricted === true);
      }
    }
    return {
      files,
      restrictedOf: (messageId) => (messageId ? restricted.get(messageId) : undefined),
      send,
    };
  }, [turns, send]);
}

/** Whether Python may run for an answer: the same decision as a preview's
 *  scripts (useArtifactsPanel in artifacts/context.ts) -- the tenant's
 *  setting first (off, or unreadable: fail closed), then restricted or
 *  unsaved answers. */
export function pythonRunMode(
  settings: { data?: { runScripts: boolean }; isError: boolean },
  restricted: boolean | undefined,
): PreviewMode {
  if (settings.data?.runScripts === false) return "off";
  if (!settings.data) return settings.isError ? "unknown" : "pending";
  if (restricted === undefined) return "unsaved";
  return restricted ? "restricted" : "scripts";
}

/** A fence language that is Python. */
export function isPythonLanguage(language: string | undefined): boolean {
  return !!language && /^(python3?|py)$/i.test(language);
}
