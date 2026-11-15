import { describe, expect, it } from "vitest";
import { WIDGET_REGISTRY } from "./widget-registry";

describe("WIDGET_REGISTRY", () => {
  it("has exactly the eight specified widget types", () => {
    expect(Object.keys(WIDGET_REGISTRY).sort()).toEqual(
      [
        "activity",
        "approvals",
        "budget",
        "chat",
        "data_board",
        "needs-me",
        "reports",
        "tasks",
      ].sort(),
    );
  });

  it("gives every entry a label function and a default size", () => {
    for (const entry of Object.values(WIDGET_REGISTRY)) {
      expect(typeof entry.label(false)).toBe("string");
      expect(entry.defaultSize.w).toBeGreaterThan(0);
      expect(entry.defaultSize.h).toBeGreaterThan(0);
    }
  });

  it("keeps the dashboard tile out of the generic picker", () => {
    const entry = WIDGET_REGISTRY.data_board;
    expect(entry.pickable).toBe(false);
    expect(entry.label(false)).toBe("Dashboard");
    expect(entry.label(true)).toBe("Dashboard");
    expect(entry.defaultSize).toEqual({ w: 6, h: 10 });
    expect(typeof entry.menu).toBe("function");
    for (const [type, e] of Object.entries(WIDGET_REGISTRY)) {
      if (type !== "data_board") expect(e.pickable).not.toBe(false);
    }
  });
});
