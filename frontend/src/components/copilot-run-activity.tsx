// The run's step timeline, in the chat, above the transcript. Reads the
// ["run", runId] cache useCopilotRunActivity seeds, kept live by the WS
// patchers in live/apply-event.ts (run.status, run.tool_call,
// run.step_timing). This component itself sits after CopilotChatTab's
// `if (!active) return null`, so it unmounts along with the rest of a
// backgrounded tab's JSX -- reactivating the tab re-fetches the run's
// current state, which is correct and cheap (staleTime is Infinity).
//
// It is a thin wrapper on purpose: the timeline itself is
// components/run-step-timeline.tsx, mounted identically by the My Work
// activity widget and the needs-me queue. One implementation, three mounts.
//
// It no longer returns null for a finished run. Throwing the record away at
// the exact moment somebody wants to review what an agent did is the
// opposite of what this surface is for.

import { useCopilotRunActivity } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { ChatMarkdown } from "@/components/chat-markdown";
import { RunStepTimeline } from "@/components/run-step-timeline";
import { TriangleAlert } from "lucide-react";

export function CopilotRunActivity({
  sessionId,
  runId,
}: {
  sessionId: string | null;
  runId: string | null;
}) {
  const t = useT();
  const { data: run } = useCopilotRunActivity(sessionId, runId);

  if (!run) return null;

  return (
    <div className="border-b border-border bg-muted/30">
      {run.state === "failed" && (
        <div className="flex flex-col gap-1 border-b border-destructive/30 bg-destructive/10 px-3 py-1.5 text-xs text-destructive">
          <div className="flex items-center gap-2">
            <TriangleAlert className="h-3.5 w-3.5" />
            <span>{t("This run failed.", "Dieser Lauf ist fehlgeschlagen.")}</span>
          </div>
          {run.output && <span className="pl-6 text-destructive/80">{run.output}</span>}
        </div>
      )}
      <RunStepTimeline
        run={{
          id: run.id,
          agentId: run.agentId,
          state: run.state,
          steps: run.steps,
          phase: run.phase,
          toolCalls: run.toolCalls,
          stepTimings: run.stepTimings,
        }}
        // Open while the run is going (somebody is watching it happen),
        // collapsed once it has finished (somebody is reading a transcript
        // and the summary row is enough until they ask).
        defaultOpen={!["done", "failed", "interrupted"].includes(run.state)}
      />
    </div>
  );
}

// Renders the assistant's answer as it streams in token by token, in the
// exact spot the "waiting" typing-dots indicator used to sit alone. Reads
// the same ["run", runId] cache CopilotRunActivity above does -- `liveAnswer`
// only exists on it once the "run.token_delta" WS patcher has concatenated a
// first fragment onto it (see hooks-chat.ts's RunActivityDTO), so there is a
// dots-only gap between "user hit send" and "first token arrived" that this
// component does not try to fill; the caller renders its own dots for that.
export function CopilotStreamingAnswer({
  sessionId,
  runId,
}: {
  sessionId: string | null;
  runId: string | null;
}) {
  const { data: run } = useCopilotRunActivity(sessionId, runId);
  if (!run?.liveAnswer) return null;

  return (
    <div className="flex gap-2">
      <img src="/octopus_oc8.svg" alt="" className="mt-0.5 h-6 w-6 shrink-0" draggable={false} />
      <ChatMarkdown text={run.liveAnswer} className="max-w-[88%]" />
    </div>
  );
}
