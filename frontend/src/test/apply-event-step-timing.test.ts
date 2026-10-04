import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { applyEvent } from "@/lib/live/apply-event";
import type { RealtimeEvent } from "@/lib/live/types";

function event(type: string, data: Record<string, unknown>): RealtimeEvent {
  return { id: "e1", type, tenantid: "t1", time: "2026-09-27T00:00:00Z", source: "test", data };
}

const timing = { step: 1, modelWaitMs: 2383, ttftMs: 412, toolWaitMs: 5, stepWallMs: 3628 };

describe("run.step_timing", () => {
  it("appends a step timing to the run's cache entry", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], { id: "r1", state: "running", steps: 1, toolCalls: [] });

    applyEvent(qc, event("run.step_timing", { run_id: "r1", timing }));

    const run = qc.getQueryData(["run", "r1"]) as {
      stepTimings?: Array<Record<string, unknown>>;
    };
    expect(run.stepTimings).toEqual([timing]);
  });

  it("appends rather than replaces on a second event", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], {
      id: "r1",
      state: "running",
      steps: 2,
      toolCalls: [],
      stepTimings: [timing],
    });

    applyEvent(qc, event("run.step_timing", { run_id: "r1", timing: { ...timing, step: 2 } }));

    const run = qc.getQueryData(["run", "r1"]) as {
      stepTimings?: Array<Record<string, unknown>>;
    };
    expect(run.stepTimings).toHaveLength(2);
    expect(run.stepTimings?.[1].step).toBe(2);
  });

  it("does nothing for a run that is not in the cache", () => {
    const qc = new QueryClient();
    applyEvent(qc, event("run.step_timing", { run_id: "unknown", timing }));
    expect(qc.getQueryData(["run", "unknown"])).toBeUndefined();
  });

  it("ignores an event with no timing payload", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], { id: "r1", state: "running", steps: 1, toolCalls: [] });
    applyEvent(qc, event("run.step_timing", { run_id: "r1" }));
    const run = qc.getQueryData(["run", "r1"]) as {
      stepTimings?: Array<Record<string, unknown>>;
    };
    expect(run.stepTimings).toBeUndefined();
  });
});
