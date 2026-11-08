// The model's reasoning for a turn: "Thinking…" while it reasons, then a
// collapsed "Thought · 4 s" the user can open. Collapsed by default (spec
// invariant) -- reasoning never pushes the answer down unasked.
import { useState } from "react";
import { ChevronRight } from "lucide-react";

import type { LiveReasoning } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** The reasoning of a run's steps (plus the step still streaming) and the
 *  model time those steps took. One filter decides which steps count, so the
 *  text and the seconds always describe the same steps. */
export function thoughtOf(run: {
  stepTimings?: Array<Record<string, unknown>>;
  liveReasoning?: LiveReasoning;
}): { text: string | null; seconds: number | null } {
  const steps = (run.stepTimings ?? []).filter(
    (entry): entry is Record<string, unknown> & { reasoning: string } =>
      typeof entry.reasoning === "string" && entry.reasoning.trim() !== "",
  );
  const parts = steps.map((entry) => entry.reasoning);
  if (run.liveReasoning?.text) parts.push(run.liveReasoning.text);
  const totalMs = steps.reduce(
    (sum, entry) => sum + (typeof entry.modelWaitMs === "number" ? entry.modelWaitMs : 0),
    0,
  );
  return {
    text: parts.length ? parts.join("\n\n") : null,
    seconds: totalMs > 0 ? Math.round(totalMs / 1000) : null,
  };
}

export function ThoughtDisclosure({
  live,
  text,
  seconds,
}: {
  live: boolean;
  text: string | null;
  seconds: number | null;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  if (!live && !text) return null;
  const label = live
    ? t("Thinking…", "Denkt nach …")
    : seconds !== null
      ? t(`Thought · ${seconds} s`, `Nachgedacht · ${seconds} s`)
      : t("Thought", "Nachgedacht");
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        disabled={!text}
        className="inline-flex items-center gap-1 rounded-md text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-default disabled:hover:text-muted-foreground"
      >
        <ChevronRight
          className={cn("size-3.5 transition-transform duration-150", open && "rotate-90")}
          aria-hidden
        />
        <span className={cn(live && "text-shimmer")}>{label}</span>
      </button>
      {open && text && (
        <p className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap border-l border-border pl-3 text-xs text-muted-foreground">
          {text}
        </p>
      )}
    </div>
  );
}
