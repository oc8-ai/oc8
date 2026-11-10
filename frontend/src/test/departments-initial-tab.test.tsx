import { afterEach, describe, expect, it } from "vitest";
import { initialDepartmentTab } from "@/lib/department-tabs";

// A run's "open guardrails" link lands on /departments/<id>?tab=guardrails.
describe("initialDepartmentTab", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("opens the tab the link names", () => {
    window.history.replaceState(null, "", "/departments/d1?tab=guardrails");
    expect(initialDepartmentTab()).toBe("guardrails");
  });

  it("falls back to the overview for anything else", () => {
    window.history.replaceState(null, "", "/departments/d1?tab=nope");
    expect(initialDepartmentTab()).toBe("overview");
    window.history.replaceState(null, "", "/departments/d1");
    expect(initialDepartmentTab()).toBe("overview");
  });
});
