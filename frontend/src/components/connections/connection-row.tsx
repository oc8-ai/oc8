import { MoreHorizontal } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

/** One compact line of a connections list: icon, name, one status line and
 *  the action(s) on the right. Rows sit in a `RowList`, separated by thin
 *  dividers -- never a card per row. */
export function ConnectionRow({
  icon,
  title,
  detail,
  warn = false,
  aside,
  children,
}: {
  icon?: ReactNode;
  title: ReactNode;
  detail?: ReactNode;
  /** The status line needs attention (shown in orange). */
  warn?: boolean;
  /** A short muted note before the actions (e.g. the last test result). */
  aside?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <li className="flex min-h-14 items-center gap-3 py-2.5">
      <span className="grid size-6 shrink-0 place-items-center">{icon}</span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{title}</p>
        {detail && (
          <p
            className={cn(
              "truncate text-xs",
              warn ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground",
            )}
          >
            {detail}
          </p>
        )}
      </div>
      {aside && <span className="shrink-0 text-xs text-muted-foreground">{aside}</span>}
      {children && <div className="flex shrink-0 items-center gap-1">{children}</div>}
    </li>
  );
}

export function RowList({ children, label }: { children: ReactNode; label?: string }) {
  return (
    <ul aria-label={label} className="divide-y divide-border/60">
      {children}
    </ul>
  );
}

export interface RowMenuItem {
  label: string;
  onSelect: () => void;
  destructive?: boolean;
  disabled?: boolean;
}

/** The overflow menu at the end of a row. Non-modal, so a confirm dialog
 *  opened from an item is not fighting the menu for focus. */
export function RowMenu({ label, items }: { label: string; items: RowMenuItem[] }) {
  if (items.length === 0) return null;
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={label} className="size-8">
          <MoreHorizontal />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {items.map((item) => (
          <DropdownMenuItem
            key={item.label}
            disabled={item.disabled}
            onSelect={item.onSelect}
            className={cn(item.destructive && "text-destructive focus:text-destructive")}
          >
            {item.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
