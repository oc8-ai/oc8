import { describe, expect, it } from "vitest";
import {
  buildRunSteps,
  GROUPING_THRESHOLD,
  MAX_RENDERED_ROWS,
  RESULT_CAP,
  type RunStepsInput,
} from "@/lib/run-steps";

const label = (call: { tool: string }) => `did ${call.tool}`;

function call(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    tool: "search_records",
    arguments: { model: "crm.lead" },
    result: "3 rows",
    step: 1,
    connection: "odoo",
    state: "done",
    durationMs: 120,
    ...over,
  };
}

function build(over: Partial<RunStepsInput> = {}) {
  return buildRunSteps({
    state: "done",
    steps: 1,
    toolCalls: [call()],
    stepTimings: [{ step: 1, stepWallMs: 1200 }],
    label,
    ...over,
  });
}

describe("rows", () => {
  it("maps one row per recorded call, in recorded order", () => {
    const steps = build({
      toolCalls: [call({ tool: "search_records" }), call({ tool: "create_record", step: 2 })],
      steps: 2,
    });
    expect(steps.rows.map((r) => r.tool)).toEqual(["search_records", "create_record"]);
    expect(steps.rows[0].label).toBe("did search_records");
  });

  it("prefers the call's own duration over the step's wall time", () => {
    expect(build().rows[0].durationMs).toBe(120);
  });

  it("falls back to the step's wall time when the call has none", () => {
    const steps = build({ toolCalls: [call({ durationMs: undefined })] });
    expect(steps.rows[0].durationMs).toBe(1200);
  });

  it("reports no duration at all rather than zero when nothing is known", () => {
    const steps = build({ toolCalls: [call({ durationMs: undefined })], stepTimings: [] });
    expect(steps.rows[0].durationMs).toBeNull();
    expect(steps.totalDurationMs).toBeNull();
  });

  it("survives a call with no name and no arguments", () => {
    const steps = build({ toolCalls: [{ step: 1 }] });
    expect(steps.rows[0].tool).toBe("");
    expect(steps.rows[0].args).toEqual({});
    expect(steps.rows[0].state).toBe("done");
  });

  it("reads a legacy `name` key when `tool` is absent", () => {
    const steps = build({ toolCalls: [{ name: "department_status", step: 1 }] });
    expect(steps.rows[0].tool).toBe("department_status");
  });

  it("marks a result at the storage cap as shortened", () => {
    const steps = build({ toolCalls: [call({ result: "x".repeat(RESULT_CAP) })] });
    expect(steps.rows[0].resultShortened).toBe(true);
    expect(build().rows[0].resultShortened).toBe(false);
  });
});

describe("state", () => {
  it("uses the recorded state", () => {
    for (const state of ["done", "failed", "denied", "awaiting_approval"] as const) {
      expect(build({ toolCalls: [call({ state })] }).rows[0].state).toBe(state);
    }
  });

  it("reads an old parked call through its `decision` key", () => {
    const steps = build({
      toolCalls: [{ tool: "create_record", decision: "require_approval", step: 1 }],
    });
    expect(steps.rows[0].state).toBe("awaiting_approval");
  });

  it("infers failed from an old record's ERROR result", () => {
    const steps = build({ toolCalls: [{ tool: "create_record", result: "ERROR: nope", step: 1 }] });
    expect(steps.rows[0].state).toBe("failed");
  });

  it("carries a denial's reason so the row can say why", () => {
    const steps = build({
      toolCalls: [call({ state: "denied", reason: "above the department limit" })],
    });
    expect(steps.rows[0].reason).toBe("above the department limit");
  });
});

describe("timings", () => {
  it("totals every step's wall time", () => {
    const steps = build({
      stepTimings: [
        { step: 1, stepWallMs: 1200 },
        { step: 2, stepWallMs: 800 },
      ],
    });
    expect(steps.totalDurationMs).toBe(2000);
  });

  it("joins out-of-order timings to the right rows", () => {
    const steps = build({
      steps: 2,
      toolCalls: [
        call({ step: 1, durationMs: undefined }),
        call({ step: 2, durationMs: undefined }),
      ],
      stepTimings: [
        { step: 2, stepWallMs: 800 },
        { step: 1, stepWallMs: 1200 },
      ],
    });
    expect(steps.rows[0].durationMs).toBe(1200);
    expect(steps.rows[1].durationMs).toBe(800);
  });

  it("ignores a timing with no usable numbers", () => {
    const steps = build({ stepTimings: [{ step: null, stepWallMs: null }] });
    expect(steps.totalDurationMs).toBeNull();
  });
});

describe("grouping", () => {
  it("stays flat at exactly the threshold", () => {
    const toolCalls = Array.from({ length: GROUPING_THRESHOLD }, (_, i) => call({ step: i + 1 }));
    expect(build({ toolCalls, steps: GROUPING_THRESHOLD }).groups).toBeNull();
  });

  it("groups one past the threshold", () => {
    const toolCalls = Array.from({ length: GROUPING_THRESHOLD + 1 }, (_, i) =>
      call({ step: i + 1 }),
    );
    const steps = build({ toolCalls, steps: GROUPING_THRESHOLD + 1 });
    expect(steps.groups).toHaveLength(1);
    expect(steps.groups?.[0].connection).toBe("odoo");
    expect(steps.groups?.[0].rows).toHaveLength(GROUPING_THRESHOLD + 1);
  });

  it("breaks a group when the connection changes and re-opens it when it changes back", () => {
    const toolCalls = [
      ...Array.from({ length: 4 }, (_, i) => call({ step: i + 1 })),
      ...Array.from({ length: 2 }, (_, i) => call({ step: i + 5, connection: "gitea" })),
      ...Array.from({ length: 3 }, (_, i) => call({ step: i + 7 })),
    ];
    const steps = build({ toolCalls, steps: 9 });
    expect(steps.groups?.map((g) => [g.connection, g.rows.length])).toEqual([
      ["odoo", 4],
      ["gitea", 2],
      ["odoo", 3],
    ]);
  });

  it("groups a control-tool stretch (no connection) on its own", () => {
    const toolCalls = [
      ...Array.from({ length: 5 }, (_, i) => call({ step: i + 1 })),
      ...Array.from({ length: 4 }, (_, i) =>
        call({ step: i + 6, tool: "memory_write", connection: undefined }),
      ),
    ];
    const steps = build({ toolCalls, steps: 9 });
    expect(steps.groups?.map((g) => g.connection)).toEqual(["odoo", null]);
  });
});

describe("degraded states", () => {
  it("says step detail is unavailable when a run has steps but no calls", () => {
    const steps = build({ toolCalls: [], steps: 15, stepTimings: [{ step: 1, stepWallMs: 10 }] });
    expect(steps.detailUnavailable).toBe(true);
    expect(steps.stepCount).toBe(15);
  });

  it("does not claim unavailable detail for a run that never took a step", () => {
    expect(build({ toolCalls: [], steps: 0, stepTimings: [] }).detailUnavailable).toBe(false);
  });

  it("caps a very long run and reports how many rows it dropped", () => {
    const toolCalls = Array.from({ length: MAX_RENDERED_ROWS + 7 }, (_, i) =>
      call({ step: i + 1 }),
    );
    const steps = build({ toolCalls, steps: MAX_RENDERED_ROWS + 7 });
    expect(steps.rows).toHaveLength(MAX_RENDERED_ROWS);
    expect(steps.truncated).toBe(7);
    // The newest rows are the ones kept.
    expect(steps.rows[steps.rows.length - 1].step).toBe(MAX_RENDERED_ROWS + 7);
  });
});

describe("liveness", () => {
  it("is live for a run that has not finished", () => {
    for (const state of ["queued", "running", "waiting_for_input", "waiting_for_approval"]) {
      expect(build({ state }).live).toBe(true);
    }
  });

  it("is not live once the run is terminal", () => {
    for (const state of ["done", "failed", "interrupted"]) {
      expect(build({ state }).live).toBe(false);
    }
  });
});
