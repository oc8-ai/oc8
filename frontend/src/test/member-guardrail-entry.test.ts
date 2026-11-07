import { describe, expect, it } from "vitest";
import type { Condition } from "@/components/guardrail-preset-picker";
import type { ToolPolicy } from "@/lib/hooks";
import { seedValue, toEntry } from "@/lib/member-guardrail-entry";

const LIMIT: Condition = {
  attribute: "amount",
  datatype: "number",
  operator: ">",
  value: 1000,
  then: "require_approval",
};

const BASE: ToolPolicy = {
  enabled: true,
  read: true,
  modify: true,
  approvalEur: 500,
  approvalActions: ["modify", "send"],
  only: null,
  conditions: [LIMIT],
};

describe("seedValue", () => {
  it("starts a member without an entry from the base rights only", () => {
    expect(seedValue({ base: BASE, mine: null })).toEqual({
      read: true,
      modify: true,
      approvalActions: [],
      approvalEur: null,
      only: [],
      conditions: [],
    });
  });

  it("starts from the member's own entry when there is one", () => {
    const mine: ToolPolicy = {
      ...BASE,
      approvalEur: 100,
      approvalActions: ["delete"],
      conditions: [],
    };
    expect(seedValue({ base: BASE, mine })).toMatchObject({
      approvalActions: ["delete"],
      approvalEur: 100,
      conditions: [],
    });
  });
});

describe("toEntry", () => {
  it("drops what the base already implies", () => {
    const entry = toEntry(
      {
        read: true,
        modify: true,
        approvalActions: ["modify", "send", "delete"],
        approvalEur: 800,
        only: [],
        conditions: [{ ...LIMIT }],
      },
      BASE,
    );
    expect(entry).toEqual({
      enabled: true,
      read: true,
      modify: true,
      approval_actions: ["delete"],
      approval_eur: null,
      conditions: [],
    });
  });

  it("keeps a stricter threshold and conditions of the member's own", () => {
    const own: Condition = { ...LIMIT, value: 50 };
    const entry = toEntry(
      {
        read: true,
        modify: false,
        approvalActions: [],
        approvalEur: 100,
        only: ["gmail.read"],
        conditions: [own],
      },
      BASE,
    );
    expect(entry).toEqual({
      enabled: true,
      read: true,
      modify: false,
      approval_actions: [],
      approval_eur: 100,
      only: ["gmail.read"],
      conditions: [own],
    });
  });

  it("keeps any threshold when the base has none", () => {
    const entry = toEntry(
      {
        read: true,
        modify: true,
        approvalActions: [],
        approvalEur: 9000,
        only: [],
        conditions: [],
      },
      { ...BASE, approvalEur: null },
    );
    expect(entry.approval_eur).toBe(9000);
  });
});
