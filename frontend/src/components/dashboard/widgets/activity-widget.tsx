// My Work's activity tile. Two things, in this order: what the caller's
// agents are doing RIGHT NOW (each with the same step timeline the chat
// shows, collapsed), then the activity feed underneath.
//
// The feed alone was never enough for "what is my agent doing": an
// activity_event carries no run id, so a feed row cannot be opened into the
// steps behind it. The runs come from GET /runs instead.

import { useActivity, useRuns } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { RunStepTimeline } from "@/components/run-step-timeline";

export function ActivityWidget(_props: {
  config: Record<string, unknown>;
  onConfigChange: (c: Record<string, unknown>) => void;
}) {
  const t = useT();
  const { data, isPending } = useActivity({ limit: 8 });
  const runsQuery = useRuns({ limit: 4 });
  const items = (data ?? []).slice(0, 8);
  const runs = (runsQuery.data ?? []).slice(0, 4);

  if (isPending && runsQuery.isPending) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-xs text-muted-foreground">
        {t("Loading…", "Wird geladen…")}
      </div>
    );
  }

  if (items.length === 0 && runs.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-xs text-muted-foreground">
        {t("No recent activity", "Keine kürzliche Aktivität")}
      </div>
    );
  }

  return (
    <div className="h-full divide-y divide-border overflow-y-auto">
      {runs.map((run) => (
        <RunStepTimeline
          key={run.id}
          run={{
            id: run.id,
            agentId: run.agentId,
            state: run.state,
            steps: run.steps,
            phase: run.phase,
            toolCalls: run.toolCalls,
            stepTimings: run.stepTimings,
          }}
        />
      ))}
      {items.map((item) => (
        <div key={item.id} className="px-3 py-2 text-xs">
          <div className="truncate font-medium">{item.message}</div>
          <div className="truncate text-muted-foreground">{item.time}</div>
        </div>
      ))}
    </div>
  );
}
