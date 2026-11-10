// Timeline runs (the Live tab's schedules, webhooks, delegations, colleagues'
// chats) as the one chat's turns render them. Feeds StepList only -- the
// conversation-line rule (steps-never-in-the-message-stream.test.ts).
import { useQueries, useQueryClient } from "@tanstack/react-query";

import type { LiveRunDTO } from "@/lib/conversation-api";
import type { RunComponentDTO } from "@/lib/hooks";
import type { RunActivityDTO } from "@/lib/hooks-chat";

const TERMINAL = new Set(["done", "failed", "interrupted"]);

function toActivity(run: LiveRunDTO, agentId: string): RunActivityDTO {
  const rendered = run.renderedComponents ?? [];
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
    renderedComponents: rendered,
    components: rendered,
  };
}

/** Cards are only ever appended, so the longer list is the fuller one: the
 *  live cache's (streamed since it was seeded) or the timeline's (persisted). */
function fullerOf(a: RunComponentDTO[] | undefined, b: RunComponentDTO[]): RunComponentDTO[] {
  return a && a.length > b.length ? a : b;
}

/** Timeline runs (Live tab) as the turns render them. A run this reader
 *  watches (`watched`) is read from its ["run", id] entry, which the WS
 *  patchers fill (steps, streamed answer, state, cards) but only update once
 *  it exists -- so it is seeded from the timeline. Once it has finished, its
 *  entry still lends the cards it streamed until the timeline carries them.
 *  A colleague's run (`redacted`), never watched and refetched instead, is
 *  only ever the timeline's own copy. */
export function useTimelineRuns(
  runs: readonly LiveRunDTO[],
  agentId: string,
  watched: ReadonlySet<string>,
): Record<string, RunActivityDTO> {
  const qc = useQueryClient();
  const readable = runs.filter((run) => !run.redacted);
  const results = useQueries({
    queries: readable.map((run) => ({
      queryKey: ["run", run.id],
      queryFn: () => qc.getQueryData<RunActivityDTO>(["run", run.id]) ?? toActivity(run, agentId),
      ...(watched.has(run.id) ? { initialData: toActivity(run, agentId) } : {}),
      enabled: false,
      staleTime: Infinity,
    })),
  });
  const cachedById = new Map(
    readable.map((run, i) => [run.id, results[i]?.data as RunActivityDTO | undefined]),
  );
  const out: Record<string, RunActivityDTO> = {};
  for (const run of runs) {
    const base = toActivity(run, agentId);
    const cached = cachedById.get(run.id);
    const components = fullerOf(cached?.components, base.renderedComponents);
    out[run.id] =
      TERMINAL.has(run.state) || !cached || !watched.has(run.id)
        ? { ...base, components }
        : { ...cached, ...base, ...pickLive(cached), components };
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
