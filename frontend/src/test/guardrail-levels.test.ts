import { describe, expect, it } from "vitest";
import {
  disabledLevelsFor,
  hasLimits,
  levelLabel,
  levelOf,
  valueForLevel,
} from "@/lib/guardrail-levels";

// The same truth table as backend `tests/authz/test_levels.py`, so the two
// mappings cannot drift apart.
const base = {
  read: true,
  modify: true,
  approvalActions: [] as string[],
  approvalEur: null,
  only: [] as string[],
  conditions: [],
};
const en = (english: string) => english;

describe("levelOf", () => {
  it.each([
    [{ ...base, enabled: false }, "off"],
    [{ ...base, read: false, modify: false }, "off"],
    [{ ...base, modify: false }, "read_only"],
    [{ ...base, approvalActions: ["modify"] }, "ask_first"],
    [base, "allowed"],
  ])("%o -> %s", (v, level) => expect(levelOf(v)).toBe(level));
});

describe("hasLimits", () => {
  it("flags details the four levels do not show", () => {
    expect(hasLimits({ ...base, approvalEur: 500 })).toBe(true);
    expect(hasLimits({ ...base, approvalActions: ["modify", "send_mail"] })).toBe(true);
    expect(hasLimits({ ...base, only: ["read_mail"] })).toBe(true);
    expect(hasLimits({ ...base, read: false })).toBe(true);
    expect(hasLimits(base)).toBe(false);
    expect(hasLimits({ ...base, approvalActions: ["modify"] })).toBe(false);
  });
});

describe("valueForLevel", () => {
  it("never loosens past the ceiling", () => {
    const ceiling = { ...base, modify: false };
    expect(valueForLevel("allowed", ceiling).modify).toBe(false);
    const approval = { ...base, approvalActions: ["modify"] };
    expect(valueForLevel("allowed", approval).approvalActions).toContain("modify");
  });

  it("maps each level onto the existing fields", () => {
    expect(levelOf(valueForLevel("off", null))).toBe("off");
    expect(levelOf(valueForLevel("read_only", null))).toBe("read_only");
    expect(levelOf(valueForLevel("ask_first", null))).toBe("ask_first");
    expect(levelOf(valueForLevel("allowed", null))).toBe("allowed");
  });

  it("keeps the details of the current value", () => {
    const current = { ...base, approvalEur: 1000, only: ["create_order"] };
    const next = valueForLevel("ask_first", null, current);
    expect(next.approvalEur).toBe(1000);
    expect(next.only).toEqual(["create_order"]);
    expect(next.approvalActions).toEqual(["modify"]);
    expect(valueForLevel("allowed", null, next).approvalActions).toEqual([]);
  });
});

it("disables every level looser than the ceiling", () => {
  expect(disabledLevelsFor({ ...base, modify: false })).toEqual(["ask_first", "allowed"]);
  expect(disabledLevelsFor(base)).toEqual([]);
  expect(disabledLevelsFor(null)).toEqual([]);
});

it("rules out read only under a write-only ceiling", () => {
  const writeOnly = { ...base, read: false, modify: true };
  expect(disabledLevelsFor(writeOnly)).toEqual(["read_only"]);
  expect(disabledLevelsFor(writeOnly, "ask_first")).toEqual(["read_only", "allowed"]);
  expect(disabledLevelsFor(base, "read_only")).toEqual(["ask_first", "allowed"]);
});

it("names the third level by scope", () => {
  expect(levelLabel("ask_first", "member", en)).toBe("Ask me first");
  expect(levelLabel("ask_first", "other", en)).toBe("Needs approval");
});
