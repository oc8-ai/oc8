import { useRef } from "react";

import { cn } from "@/lib/utils";

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
