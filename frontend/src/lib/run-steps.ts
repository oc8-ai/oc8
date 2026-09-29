// Joins a run's call records, its per-step timings and its resolved labels
// into the one shape the timeline renders. Split out of the component on
// purpose: the grouping rule and the state/duration fallbacks are where the
// bugs will be, and neither needs a DOM to be tested.

import type { LabelledCall } from "@/lib/tool-labels";

/** Below this many rows a timeline reads better flat; above it, consecutive
 *  calls against the same connection collapse under one header. Neither
 *  always-grouped nor never-grouped: a 3-step run grouped is noise, a
 *  25-step run flat is a wall. */
export const GROUPING_THRESHOLD = 8;

/** A very long run's timeline is capped; the operator run drawer is where a
 *  complete list belongs. The newest rows are the ones kept. */
export const MAX_RENDERED_ROWS = 50;

/** Both runtimes store `result: output[:300]`. A result at exactly the cap
 *  was almost certainly cut, and saying so is more honest than pretending
 *  the row shows everything -- there is no longer copy stored anywhere. */
export const RESULT_CAP = 300;

const TERMINAL_RUN_STATES = ["done", "failed", "interrupted"];

export type StepState = "running" | "done" | "failed" | "denied" | "awaiting_approval";

export interface RunStepRow {
  /** Stable enough for a React key: position plus step plus tool. */
  key: string;
  step: number | null;
  tool: string;
  label: string;
  state: StepState;
  connection: string | null;
  /** The call's own duration when it has one, else the whole step's wall
   *  time, else null -- rendered as nothing, never as 0. */
  durationMs: number | null;
  args: Record<string, unknown>;
  result: string | null;
  resultShortened: boolean;
  reason: string | null;
}

export interface RunStepGroup {
  connection: string | null;
  rows: RunStepRow[];
}

export interface RunSteps {
  rows: RunStepRow[];
  /** null means "render flat" (at or below GROUPING_THRESHOLD rows). */
  groups: RunStepGroup[] | null;
  stepCount: number;
  totalDurationMs: number | null;
  /** The run took steps but recorded no calls -- an old run, or a runtime
   *  whose step detail never reached the record. The timeline says so
   *  explicitly: silently showing zero steps for a run that did fifteen is
   *  worse than saying nothing. */
  detailUnavailable: boolean;
  /** How many rows the cap dropped. */
  truncated: number;
  /** The run is still going, so a trailing in-progress row belongs at the
   *  bottom and durations are still arriving. */
  live: boolean;
}

export interface RunStepsInput {
  state: string;
  steps: number;
  toolCalls: Array<Record<string, unknown>>;
  stepTimings?: Array<Record<string, unknown>>;
  /** Injected so this module needs neither i18n nor a fetch. */
  label: (call: LabelledCall) => string;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function rowState(call: Record<string, unknown>): StepState {
  const recorded = str(call.state);
  if (
    recorded === "done" ||
    recorded === "failed" ||
    recorded === "denied" ||
    recorded === "awaiting_approval"
  ) {
    return recorded;
  }
  // Records written before the state field existed. The inference is kept
  // ONLY for those: it cannot tell a refusal from a failure (both read
  // `ERROR: ...`), which is exactly why the field was added.
  if (str(call.decision) === "require_approval") return "awaiting_approval";
  return (str(call.result) ?? "").startsWith("ERROR:") ? "failed" : "done";
}

function toRow(
  call: Record<string, unknown>,
  index: number,
  wallByStep: Map<number, number>,
  label: (call: LabelledCall) => string,
): RunStepRow {
  const tool = str(call.tool) ?? str(call.name) ?? "";
  const rawArgs = call.arguments;
  const args =
    rawArgs !== null && typeof rawArgs === "object" && !Array.isArray(rawArgs)
      ? (rawArgs as Record<string, unknown>)
      : {};
  const connection = str(call.connection);
  const step = num(call.step);
  const result = str(call.result);
  return {
    key: `${index}-${step ?? "x"}-${tool}`,
    step,
    tool,
    connection,
    args,
    label: label({ tool, arguments: args, connection }),
    state: rowState(call),
    durationMs: num(call.durationMs) ?? (step !== null ? (wallByStep.get(step) ?? null) : null),
    result,
    resultShortened: (result?.length ?? 0) >= RESULT_CAP,
    reason: str(call.reason),
  };
}

function group(rows: RunStepRow[]): RunStepGroup[] {
  const groups: RunStepGroup[] = [];
  for (const row of rows) {
    const last = groups[groups.length - 1];
    if (last && last.connection === row.connection) last.rows.push(row);
    else groups.push({ connection: row.connection, rows: [row] });
  }
  return groups;
}

export function buildRunSteps(input: RunStepsInput): RunSteps {
  const wallByStep = new Map<number, number>();
  let total: number | null = null;
  for (const timing of input.stepTimings ?? []) {
    const step = num(timing.step);
    const wall = num(timing.stepWallMs);
    // Out-of-order or duplicated step numbers: last one wins, and the total
    // still counts every entry, because the total is "how long did this run
    // spend", not "how long was each distinct step".
    if (step !== null && wall !== null) wallByStep.set(step, wall);
    if (wall !== null) total = (total ?? 0) + wall;
  }

  const all = input.toolCalls.map((call, i) => toRow(call, i, wallByStep, input.label));
  const truncated = Math.max(0, all.length - MAX_RENDERED_ROWS);
  const rows = truncated > 0 ? all.slice(all.length - MAX_RENDERED_ROWS) : all;

  return {
    rows,
    groups: rows.length > GROUPING_THRESHOLD ? group(rows) : null,
    stepCount: input.steps,
    totalDurationMs: total,
    detailUnavailable: input.toolCalls.length === 0 && input.steps > 0,
    truncated,
    live: !TERMINAL_RUN_STATES.includes(input.state),
  };
}
