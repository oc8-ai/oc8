// An answer as it streams: markdown plus a small breathing dot after the last
// word until it is done. Fragments arrive in bursts; useSmoothReveal shows
// them at a steady, catching-up pace, word by word, and the words that just
// appeared fade in (one span, see stream-words.ts). The dot is the markdown
// root's own ::after on its last block (see `@utility stream-tail`), so it
// sits inline after the last word and adds no box of its own -- nothing
// jumps when it goes away. Before the first word the root is empty and draws
// nothing: ThinkingPresence speaks for the turn then. Announced politely;
// aria-busy keeps screen readers from reading every token. Under reduced
// motion the text shows as it arrives, without fades.
import { ChatMarkdown } from "@/components/chat-markdown";
import type { ChatSourceDTO } from "@/lib/hooks-chat";

import { usePrefersReducedMotion, useSmoothReveal } from "./smooth-reveal";

export function StreamingText({
  text,
  streaming,
  messageId,
  sources,
  onCitation,
  onObjectClick,
}: {
  text: string;
  streaming: boolean;
  /** The saved answer, once there is one (see ChatMarkdown): until then its
   *  links stay text. */
  messageId?: string;
  sources?: ChatSourceDTO[];
  onCitation?: (n: number) => void;
  onObjectClick?: (href: string) => void;
}) {
  const reducedMotion = usePrefersReducedMotion();
  const shown = useSmoothReveal(text, streaming && !reducedMotion);
  if (!text && !streaming) return null;
  return (
    <div aria-live="polite" aria-busy={streaming}>
      <ChatMarkdown
        text={shown}
        sources={sources}
        onCitation={onCitation}
        onObjectClick={onObjectClick}
        streaming={streaming}
        messageId={messageId}
      />
    </div>
  );
}
