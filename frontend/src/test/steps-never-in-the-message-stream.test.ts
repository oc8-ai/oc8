// The conversation-line rule (run step timeline design §2.4, workplace design
// §3.2): an agent action produces ONE line in the conversation and its detail
// in the activity surface. Messages render text; the timeline renders steps.
//
// Enforced as a source scan rather than a render assertion because the defect
// it prevents is a NEW component reading `toolCalls` and putting a step into a
// message list -- which no test of today's components can catch. Same shape as
// the backend's own tests/architecture/test_import_boundaries.py.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(process.cwd(), "src");

/** The only files allowed to read a run's tool-call records.
 *  - lib/run-steps.ts builds the view model,
 *  - components/run-step-timeline.tsx renders it,
 *  - components/copilot-run-activity.tsx + the two widgets mount it,
 *  - routes/agents.$id.tsx is the OPERATOR run drawer, a different surface
 *    with a different audience (see the spec's non-goals),
 *  - lib/live/apply-event.ts patches the cache,
 *  - lib/hooks*.ts declare the DTO field. */
const ALLOWED = new Set([
  "lib/run-steps.ts",
  "lib/hooks.ts",
  "lib/hooks-chat.ts",
  "lib/live/apply-event.ts",
  "components/run-step-timeline.tsx",
  "components/copilot-run-activity.tsx",
  "components/dashboard/widgets/activity-widget.tsx",
  "components/dashboard/widgets/needs-me-widget.tsx",
  "routes/agents.$id.tsx",
]);

function sources(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      sources(full, acc);
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      acc.push(full);
    }
  }
  return acc;
}

describe("the conversation-line invariant", () => {
  it("only the timeline and its mounts read a run's tool calls", () => {
    const offenders = sources(SRC)
      .filter((path) => readFileSync(path, "utf8").includes("toolCalls"))
      .map((path) =>
        path
          .slice(SRC.length + 1)
          .split("\\")
          .join("/"),
      )
      .filter((rel) => !ALLOWED.has(rel));
    expect(offenders).toEqual([]);
  });

  it("the chat's own message list does not mention a tool call", () => {
    // copilot-dock.tsx renders the transcript. It may mount the timeline
    // (it does, at the top) but must not read a step itself.
    const dock = readFileSync(join(SRC, "components", "copilot-dock.tsx"), "utf8");
    expect(dock).not.toContain("toolCalls");
  });
});
