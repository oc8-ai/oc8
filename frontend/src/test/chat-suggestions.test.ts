import { describe, expect, it } from "vitest";

import { nextStepChips, type ChipInputs } from "@/lib/chat-suggestions";

const en = (text: string) => text;
const BASE: ChipInputs = {
  hasMessages: true,
  lastTurnRole: "assistant",
  lastUserMode: null,
  pendingApprovals: 0,
  budgetSoftExceeded: false,
  mayViewBudget: true,
  promptStarters: [],
};

describe("next-step chips", () => {
  it("shows the agent's own prompt starters on an empty conversation", () => {
    const chips = nextStepChips(
      { ...BASE, hasMessages: false, promptStarters: ["Wie viele Tickets sind offen?"] },
      en,
    );
    expect(chips.map((c) => c.insert)).toEqual(["Wie viele Tickets sind offen?"]);
  });

  it("falls back to generic starters for an agent that ships none", () => {
    const chips = nextStepChips({ ...BASE, hasMessages: false }, en);
    expect(chips.length).toBeGreaterThan(0);
    expect(chips.length).toBeLessThanOrEqual(3);
  });

  it("offers to carry out a plan the agent just produced", () => {
    const chips = nextStepChips({ ...BASE, lastUserMode: "plan" }, en);
    expect(chips[0].insert.startsWith("/do ")).toBe(true);
  });

  it("does not offer to carry out a plan while the agent is still working", () => {
    // lastTurnRole === "user" means the answering run has not landed yet.
    const chips = nextStepChips({ ...BASE, lastUserMode: "plan", lastTurnRole: "user" }, en);
    expect(chips.some((c) => c.insert.startsWith("/do "))).toBe(false);
  });

  it("surfaces waiting approvals with the real count", () => {
    const chips = nextStepChips({ ...BASE, pendingApprovals: 3 }, en);
    expect(chips.some((c) => c.label.includes("3"))).toBe(true);
  });

  it("offers the budget only when the caller may read it", () => {
    expect(
      nextStepChips({ ...BASE, budgetSoftExceeded: true }, en).some((c) => c.insert === "/budget"),
    ).toBe(true);
    expect(
      nextStepChips({ ...BASE, budgetSoftExceeded: true, mayViewBudget: false }, en).some(
        (c) => c.insert === "/budget",
      ),
    ).toBe(false);
  });

  it("always offers a summary once there is something to summarise", () => {
    expect(nextStepChips(BASE, en).some((c) => c.insert.startsWith("/summarise"))).toBe(true);
  });

  it("never shows more than three", () => {
    const chips = nextStepChips(
      {
        ...BASE,
        lastUserMode: "plan",
        pendingApprovals: 9,
        budgetSoftExceeded: true,
        promptStarters: ["a", "b", "c", "d"],
      },
      en,
    );
    expect(chips.length).toBeLessThanOrEqual(3);
  });

  it("is deterministic: the same state gives the same chips", () => {
    // §5.3: computed from state, never model-generated. No clock, no random,
    // no network -- this assertion is what keeps that true.
    const input = { ...BASE, pendingApprovals: 2, lastUserMode: "plan" };
    expect(nextStepChips(input, en)).toEqual(nextStepChips(input, en));
  });

  it("gives every chip a stable id so React keys do not thrash", () => {
    const chips = nextStepChips({ ...BASE, pendingApprovals: 2 }, en);
    expect(new Set(chips.map((c) => c.id)).size).toBe(chips.length);
  });
});
