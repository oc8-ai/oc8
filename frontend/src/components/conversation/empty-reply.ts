import type { ChatMessageDTO } from "@/lib/hooks-chat";

/** A finished answer that carries nothing to show: no text, no parts (an
 *  agent_builder card, answers, suggestions, sources ride as parts) and no
 *  run components. The transcript keeps it empty; the turn says so instead
 *  of rendering nothing, so the member knows it ended. */
export function isEmptyReply(message: ChatMessageDTO | null): boolean {
  if (!message || message.role !== "assistant") return false;
  return (
    !message.content.trim() &&
    (message.parts ?? []).length === 0 &&
    message.renderedComponents.length === 0 &&
    (message.suggestions ?? []).length === 0
  );
}
