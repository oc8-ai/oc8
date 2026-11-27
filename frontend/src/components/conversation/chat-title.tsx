import { MessageCircleDashed } from "lucide-react";
import { useRef } from "react";

import { cn } from "@/lib/utils";

import { useTemporaryChatText } from "./new-chat-label";

/** A chat's title in a list or picker. It fades in when it changes while on
 *  screen -- a generated title arriving live, a rename -- but not on first
 *  paint, so opening a list does not flicker every row. */
export function ChatTitleText({ title, className }: { title: string; className?: string }) {
  const first = useRef(title);
  const changed = title !== first.current;
  return (
    <span
      // A new key remounts the span, which replays the fade for each change.
      key={changed ? title : undefined}
      data-title-changed={changed ? "" : undefined}
      className={cn(
        changed && "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-500",
        className,
      )}
    >
      {title}
    </span>
  );
}

/** The mark of a temporary chat (spec §5.6), above its transcript: it is not
 *  kept, and the tooltip says when it goes. */
export function TemporaryChatChip({ className }: { className?: string }) {
  const { chip, help } = useTemporaryChatText();
  return (
    <span
      title={help}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-dashed border-border px-2.5 py-0.5 text-xs text-muted-foreground",
        className,
      )}
    >
      <MessageCircleDashed className="size-3.5" aria-hidden />
      <span>{chip}</span>
      <span className="sr-only">{help}</span>
    </span>
  );
}
