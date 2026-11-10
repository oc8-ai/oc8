// The turn before its first word: a small Copilot mark in the brand colour
// that breathes, next to one line saying what the run is doing ("Thinking …",
// "Reading www.example.com …", "Starting the workspace …"). It replaces the
// empty caret, not the step list -- StepList still shows every step; this
// line only says what is happening now. The mark is decoration (aria-hidden);
// the line is a polite status that changes, and so is announced, only when
// its words change. Under reduced motion the mark stands still and the words
// swap without a fade.
import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";

import { useLang, useT } from "@/lib/i18n";
import { SLOW_START_TEXT, isStartPhase, useSlowStart } from "@/lib/run-start-phase";
import { useToolLabels } from "@/lib/tool-labels";
import { cn } from "@/lib/utils";

import { latestCall, presenceLabel, type PresenceLabel, type PresenceRun } from "./presence-label";

/** How long the outgoing words stay while they fade (matches presence-out). */
const CROSSFADE_MS = 200;

/** The breathing mark, on its own so other waiting states can reuse it. */
export function PresenceMark({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      data-testid="presence-mark"
      className={cn(
        "presence-mark grid size-6 shrink-0 place-items-center rounded-full bg-primary/10 text-primary motion-reduce:animate-none",
        className,
      )}
    >
      <Sparkles className="size-3.5" />
    </span>
  );
}

/** An agent's name from whatever the client already has cached, never a new
 *  request: a colleague's agent may not be readable, and the line must not
 *  wait for a fetch. */
function useCachedAgentName(): (agentId: string) => string | null {
  const qc = useQueryClient();
  return (agentId) => {
    for (const [, data] of qc.getQueriesData<unknown>({ queryKey: ["agents"] })) {
      const items = Array.isArray(data) ? data : [data];
      for (const item of items) {
        if (item && typeof item === "object") {
          const { id, name } = item as { id?: unknown; name?: unknown };
          if (id === agentId && typeof name === "string" && name) return name;
        }
      }
    }
    return null;
  };
}

export function ThinkingPresence({ run }: { run: PresenceRun | null }) {
  const t = useT();
  const { lang } = useLang();
  const call = run ? latestCall(run) : null;
  const connection =
    call && typeof call.connection === "string" && call.connection ? call.connection : null;
  // Same query StepList runs, so it is served from the cache.
  const { data: catalogue } = useToolLabels(connection);
  const agentName = useCachedAgentName();
  const label = presenceLabel(run, { t, lang, catalogue, agentName });
  const starting = !!run && isStartPhase(run.phase);
  const slow = useSlowStart(starting);

  // The words on their way out, kept for one crossfade. Set while
  // rendering (React's "adjust state on a prop change"), so the swap and the
  // fade start in the same frame.
  const [current, setCurrent] = useState(label);
  const [outgoing, setOutgoing] = useState<PresenceLabel | null>(null);
  if (current.key !== label.key || current.text !== label.text) {
    setOutgoing(current);
    setCurrent(label);
  }
  useEffect(() => {
    if (!outgoing) return;
    const timer = window.setTimeout(() => setOutgoing(null), CROSSFADE_MS);
    return () => window.clearTimeout(timer);
  }, [outgoing]);

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="thinking-presence"
      className="text-sm text-muted-foreground"
    >
      <div className="flex items-center gap-2">
        <PresenceMark />
        <span className="grid min-w-0">
          {outgoing && (
            <span
              aria-hidden
              className="presence-out col-start-1 row-start-1 truncate motion-reduce:hidden"
            >
              {outgoing.text}
            </span>
          )}
          <span
            key={`${label.key}:${label.text}`}
            className="presence-in col-start-1 row-start-1 min-w-0 truncate"
          >
            {label.text}
          </span>
        </span>
      </div>
      {slow && <p className="mt-1 pl-8 text-xs">{t(SLOW_START_TEXT.en, SLOW_START_TEXT.de)}</p>}
    </div>
  );
}
