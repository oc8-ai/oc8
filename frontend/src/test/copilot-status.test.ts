import { describe, expect, it } from "vitest";
import { mascotState, statusLabel } from "@/lib/copilot-status";

const en = (e: string) => e;
const de = (_e: string, d?: string) => d ?? _e;

describe("statusLabel", () => {
  it("covers all four states in both languages", () => {
    expect(statusLabel(en, "ready", 0)).toBe("Ready");
    expect(statusLabel(en, "working", 1)).toBe("Working on 1 thing");
    expect(statusLabel(en, "working", 3)).toBe("Working on 3 things");
    expect(statusLabel(de, "working", 3)).toBe("Arbeitet an 3 Dingen");
    expect(statusLabel(de, "waiting", 1)).toBe("Wartet auf dich");
    expect(statusLabel(de, "paused", 0)).toBe("Pausiert");
  });
  it("maps status to a mascot state", () => {
    expect(mascotState("ready")).toBe("idle");
    expect(mascotState("waiting")).toBe("waiting");
  });
});
