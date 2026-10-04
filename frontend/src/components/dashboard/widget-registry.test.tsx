import { describe, expect, it } from "vitest";
import { WIDGET_REGISTRY } from "./widget-registry";

describe("WIDGET_REGISTRY", () => {
  it("has exactly the seven specified widget types", () => {
    expect(Object.keys(WIDGET_REGISTRY).sort()).toEqual(
      ["activity", "approvals", "budget", "chat", "needs-me", "reports", "tasks"].sort(),
    );
  });

  it("gives every entry a label function and a default size", () => {
    for (const entry of Object.values(WIDGET_REGISTRY)) {
      expect(typeof entry.label(false)).toBe("string");
      expect(entry.defaultSize.w).toBeGreaterThan(0);
      expect(entry.defaultSize.h).toBeGreaterThan(0);
    }
  });
});
