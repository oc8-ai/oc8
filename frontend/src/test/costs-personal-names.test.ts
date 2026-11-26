// frontend/src/test/costs-personal-names.test.ts
// A member's personal agent and its department are left out of an admin's
// agent and department lists, but their names are metadata the admin keeps
// (R2). The usage rows carry them, so the Costs page names the row rather
// than showing a raw id, and the department counts the agent.
import { describe, expect, it } from "vitest";
import { buildAgentRows, buildDeptRows } from "@/routes/costs";
import type { UsageDTO } from "@/lib/hooks";

const AGENT_ID = "01a20fd0-0000-7000-8000-000000000001";
const DEPT_ID = "01a20cda-0000-7000-8000-000000000002";

function usage(group: string, names: Partial<UsageDTO>): UsageDTO {
  return {
    group,
    tokensIn: 10,
    tokensOut: 5,
    providerCostMicros: 2_000_000,
    savedTokensIn: 0,
    savedTokensOut: 0,
    savedCostMicros: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    ...names,
  };
}

describe("costs rows of an agent missing from the agent list", () => {
  it("take the agent and department names from the usage row", () => {
    const rows = buildAgentRows(
      [usage(AGENT_ID, { name: "Mailbot", departmentId: DEPT_ID, departmentName: "Justin" })],
      [],
      [],
      "Unassigned",
    );
    expect(rows[0].name).toBe("Mailbot");
    expect(rows[0].departmentId).toBe(DEPT_ID);
    expect(rows[0].departmentName).toBe("Justin");
  });

  it("name the department from the usage row", () => {
    const rows = buildDeptRows([usage(DEPT_ID, { name: "Justin" })], [], "Unassigned");
    expect(rows[0].name).toBe("Justin");
  });

  it("still fall back to the id when the row names nothing", () => {
    const rows = buildAgentRows([usage(AGENT_ID, {})], [], [], "Unassigned");
    expect(rows[0].name).toBe(AGENT_ID);
    expect(rows[0].departmentName).toBeNull();
  });
});
