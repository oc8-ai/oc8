// An answer as it streams: markdown plus a blinking caret until it is done.
// Announced politely; aria-busy keeps screen readers from reading every token.
import { ChatMarkdown } from "@/components/chat-markdown";
import type { ChatSourceDTO } from "@/lib/hooks-chat";

export function StreamingText({
  text,
  streaming,
  sources,
  onCitation,
}: {
  text: string;
  streaming: boolean;
  sources?: ChatSourceDTO[];
  onCitation?: (n: number) => void;
}) {
  if (!text && !streaming) return null;
  return (
    <div aria-live="polite" aria-busy={streaming} className="relative">
      <ChatMarkdown text={text} sources={sources} onCitation={onCitation} />
      {streaming && <span data-testid="stream-caret" className="stream-caret" aria-hidden />}
    </div>
  );
}
