// The small icon buttons under an answer (copy, regenerate, rate, read aloud).
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function ActionButton({
  label,
  onClick,
  children,
  pressed,
  disabled,
  describedBy,
  title,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
  pressed?: boolean;
  disabled?: boolean;
  /** The id of what explains a disabled button. */
  describedBy?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={pressed}
      aria-describedby={describedBy}
      disabled={disabled}
      title={title}
      className={cn(
        "grid size-9 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent",
        pressed && "text-foreground",
      )}
    >
      {children}
    </button>
  );
}
