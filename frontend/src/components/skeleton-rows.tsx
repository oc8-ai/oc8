import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * Loading placeholder in the shape of a list or a block of text (DESIGN.md
 * "States": a skeleton, not a spinner, while content loads). The label is
 * read out to screen readers in place of the old "Loading…" line.
 */
export function SkeletonRows({
  label,
  rows = 3,
  className,
  rowClassName = "h-4",
}: {
  label: string;
  rows?: number;
  className?: string;
  rowClassName?: string;
}) {
  return (
    <div role="status" aria-live="polite" className={cn("space-y-2", className)}>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className={cn(rowClassName, rows > 1 && i === rows - 1 && "w-2/3")} />
      ))}
      <span className="sr-only">{label}</span>
    </div>
  );
}
