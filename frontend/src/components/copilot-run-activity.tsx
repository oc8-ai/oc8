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
import { ChatMarkdown } from "@/components/chat-markdown";
import { ConnectionRequiredNotice } from "@/components/connections/connection-required-notice";
import { ToolDeniedNotice } from "@/components/connections/tool-denied-notice";
import { RunFailureReason } from "@/components/run-reasoning";
import { RunStepTimeline } from "@/components/run-step-timeline";

export function CopilotRunActivity({
  sessionId,
  runId,
}: {
  sessionId: string | null;
  runId: string | null;
}) {
  const { data: run } = useCopilotRunActivity(sessionId, runId);

  if (!run) return null;

  return (
    <div className="border-b border-border bg-muted/30">
      {run.state === "failed" && (
        <RunFailureReason
          reason={run.failureReason ?? run.output}
          className="rounded-none border-x-0 border-t-0"
        />
      )}
      {run.connectionRequired && (
        <div className="px-3 py-2">
          <ConnectionRequiredNotice value={run.connectionRequired} agentId={run.agentId} />
        </div>
      )}
      {run.toolDenied && (
        <div className="px-3 py-2">
          <ToolDeniedNotice value={run.toolDenied} />
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
          liveReasoning: run.liveReasoning,
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
