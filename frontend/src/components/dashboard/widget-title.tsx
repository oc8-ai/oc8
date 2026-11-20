import type { ReactNode } from "react";

/** The one-line title every widget draws as its first line. The tile frame is
 * the only bordered box, so this has no border of its own. */
export function WidgetTitle({ title, actions }: { title: string; actions?: ReactNode }) {
  return (
    <div className="flex shrink-0 items-center gap-2 px-3 pt-2 pb-1">
      <span className="min-w-0 flex-1 truncate text-xs font-medium text-muted-foreground">
        {title}
      </span>
      {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
    </div>
  );
}
