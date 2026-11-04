import { describe, expect, it } from "vitest";
import { mergePolledRun, type RunDTO } from "@/lib/hooks";

function run(partial: Partial<RunDTO> & Pick<RunDTO, "state">): RunDTO {
  return {
    id: "r1",
    agentId: "a1",
    steps: 1,
    toolCalls: [],
    state: partial.state,
    phase: partial.phase,
    updatedAt: partial.updatedAt ?? "2026-09-27T12:00:00Z",
    liveAnswer: partial.liveAnswer,
  } as RunDTO;
}

describe("mergePolledRun", () => {
  it("keeps streamed text when the poll has none", () => {
    const merged = mergePolledRun(
      run({ state: "running", liveAnswer: "Hallo" }),
      run({ state: "running", updatedAt: "2026-09-27T12:00:20Z" }),
    );
    expect(merged.liveAnswer).toBe("Hallo");
    expect(merged.updatedAt).toBe("2026-09-27T12:00:20Z");
  });

  it("leaves a poll alone when nothing has streamed yet", () => {
    const fresh = run({ state: "running" });
    expect(mergePolledRun(undefined, fresh)).toBe(fresh);
  });
});

describe("mergePolledRun with a run.status state", () => {
  it("keeps the state the last event announced over a stale poll", () => {
    // The executor publishes RUNNING before it commits it, so the GET still
    // reads "queued" for a run the header already shows as running.
    const merged = mergePolledRun(undefined, run({ state: "queued" }), {
      state: "running",
      phase: "step",
    });
    expect(merged.state).toBe("running");
    expect(merged.phase).toBe("step");
  });

  it("leaves a poll that agrees with the event alone", () => {
    const fresh = run({ state: "done" });
    expect(mergePolledRun(undefined, fresh, { state: "done", phase: null })).toBe(fresh);
  });
});
