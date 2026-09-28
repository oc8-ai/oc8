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
