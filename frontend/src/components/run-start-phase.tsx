import { Loader2 } from "lucide-react";
import { useT } from "@/lib/i18n";
import { isTerminalRunState } from "@/lib/hooks";
import { SLOW_START_TEXT, START_TEXT, isStartPhase, useSlowStart } from "@/lib/run-start-phase";
import { cn } from "@/lib/utils";

/** "Starting the workspace …" while the run waits for its sandbox, and after
 *  20 s a second line saying a cold start takes up to three minutes. Shown
 *  at once, so a run never sits there looking like nothing happens. */
export function RunStartPhase({
  phase,
  state,
  className,
}: {
  phase?: string | null;
  state: string;
  className?: string;
}) {
  const t = useT();
  const active = isStartPhase(phase) && !isTerminalRunState(state);
  const slow = useSlowStart(active);
  if (!active) return null;
  return (
    <div
      role="status"
      className={cn(
        "rounded-md border border-border bg-background/40 px-3 py-2 text-sm",
        className,
      )}
    >
      <div className="flex items-center gap-2">
        <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden />
        <span>{t(START_TEXT.en, START_TEXT.de)}</span>
      </div>
      {slow && (
        <p className="mt-1 pl-5 text-xs text-muted-foreground">
          {t(SLOW_START_TEXT.en, SLOW_START_TEXT.de)}
        </p>
      )}
    </div>
  );
}
