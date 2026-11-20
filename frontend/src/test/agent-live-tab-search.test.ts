// frontend/src/test/agent-live-tab-search.test.ts
// The agent page opens a tab and a run from its search ("Technical view" on
// the Live tab links to the Live Log with that run).
import { describe, expect, it } from "vitest";

import { Route } from "@/routes/agents.$id";

const validate = (search: Record<string, unknown>) =>
  (Route.options.validateSearch as (s: Record<string, unknown>) => unknown)(search);

describe("agent page search", () => {
  const id = "6f1c2b9e-3d4a-4c5b-9e8f-0a1b2c3d4e5f";

  it("opens the Live Log with a run", () => {
    expect(validate({ tab: "livelog", runId: id })).toEqual({ tab: "livelog", runId: id });
  });

  it("ignores a run id that is not one", () => {
    expect(validate({ tab: "livelog", runId: "../../members/x" })).toEqual({ tab: "livelog" });
  });

  it("sends the old chat tab to Live", () => {
    expect(validate({ tab: "chat" })).toEqual({ tab: "live" });
  });

  it("drops a tab it does not know", () => {
    expect(validate({ tab: "nope", runId: "" })).toEqual({});
  });
});
