// Timeline runs (the Live tab's schedules, webhooks, delegations, colleagues'
// chats) as the one chat's turns render them. Feeds StepList only -- the
// conversation-line rule (steps-never-in-the-message-stream.test.ts).
import { useQueries, useQueryClient } from "@tanstack/react-query";

import type { LiveRunDTO } from "@/lib/conversation-api";
import type { RunActivityDTO } from "@/lib/hooks-chat";

const TERMINAL = new Set(["done", "failed", "interrupted"]);

function toActivity(run: LiveRunDTO, agentId: string): RunActivityDTO {
  return {
    id: run.id,
    agentId,
    state: run.state,
    phase: null,
    output: run.output,
    steps: run.steps,
    toolCalls: run.toolCalls,
    stepTimings: run.stepTimings,
    taskId: null,
    question: null,
    renderedComponents: [],
  };
}

/** Timeline runs (Live tab) as the turns render them. A run this reader
 *  watches (`watched`) is read from its ["run", id] entry, which the WS
 *  patchers fill (steps, streamed answer, state) but only update once it
 *  exists -- so it is seeded from the timeline. Any other run -- one that has
 *  ended, or a colleague's, which is never watched and is refetched instead --
 *  is the timeline's own copy. */
export function useTimelineRuns(
  runs: readonly LiveRunDTO[],
  agentId: string,
  watched: ReadonlySet<string>,
): Record<string, RunActivityDTO> {
  const qc = useQueryClient();
  const live = runs.filter((run) => watched.has(run.id));
  const results = useQueries({
    queries: live.map((run) => ({
      queryKey: ["run", run.id],
      queryFn: () => qc.getQueryData<RunActivityDTO>(["run", run.id]) ?? toActivity(run, agentId),
      initialData: toActivity(run, agentId),
      enabled: false,
      staleTime: Infinity,
    })),
  });
  const cachedById = new Map(
    live.map((run, i) => [run.id, results[i]?.data as RunActivityDTO | undefined]),
  );
  const out: Record<string, RunActivityDTO> = {};
  for (const run of runs) {
    const base = toActivity(run, agentId);
    const cached = cachedById.get(run.id);
    out[run.id] =
      TERMINAL.has(run.state) || !cached
        ? { ...base, components: cached?.components }
        : { ...cached, ...base, ...pickLive(cached) };
  }
  return out;
}

/** What the live patchers know better than a timeline fetch of a running run. */
export function pickLive(cached: RunActivityDTO): Partial<RunActivityDTO> {
  const live: Partial<RunActivityDTO> = { state: cached.state, phase: cached.phase };
  if (cached.steps) live.steps = cached.steps;
  if (cached.toolCalls?.length) live.toolCalls = cached.toolCalls;
  if (cached.stepTimings?.length) live.stepTimings = cached.stepTimings;
  return live;
}
