// An answer as it streams: markdown plus a blinking caret until it is done.
// The caret is the markdown root's own ::after on its last block (see
// `@utility stream-caret`), so it sits inline after the last word and adds no
// box of its own -- nothing jumps when it goes away. Announced politely;
// aria-busy keeps screen readers from reading every token.
import { ChatMarkdown } from "@/components/chat-markdown";
import type { ChatSourceDTO } from "@/lib/hooks-chat";

export function StreamingText({
  text,
  streaming,
  sources,
  onCitation,
  onObjectClick,
}: {
  text: string;
  streaming: boolean;
  sources?: ChatSourceDTO[];
  onCitation?: (n: number) => void;
  onObjectClick?: (href: string) => void;
}) {
  if (!text && !streaming) return null;
  return (
    <div aria-live="polite" aria-busy={streaming}>
      <ChatMarkdown
        text={text}
        sources={sources}
        onCitation={onCitation}
        onObjectClick={onObjectClick}
        streaming={streaming}
      />
    </div>
  );
}
