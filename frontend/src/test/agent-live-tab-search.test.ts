// frontend/src/test/agent-live-tab-search.test.ts
// The agent page opens a tab and a run from its search ("Technical view" on
// the Live tab links to the Live Log with that run).
import { describe, expect, it } from "vitest";

import { Route } from "@/routes/agents.$id";

const validate = (search: Record<string, unknown>) =>
  (Route.options.validateSearch as (s: Record<string, unknown>) => unknown)(search);

describe("agent page search", () => {
  it("opens the Live Log with a run", () => {
    expect(validate({ tab: "livelog", runId: "r1" })).toEqual({ tab: "livelog", runId: "r1" });
  });

  it("sends the old chat tab to Live", () => {
    expect(validate({ tab: "chat" })).toEqual({ tab: "live" });
  });

  it("drops a tab it does not know", () => {
    expect(validate({ tab: "nope", runId: "" })).toEqual({});
  });
});
