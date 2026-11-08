import { describe, expect, it } from "vitest";

import { stateLabel } from "@/lib/run-state";

const de = (_en: string, deText?: string) => deText ?? _en;
const en = (enText: string) => enText;

describe("stateLabel", () => {
  it("names responsibility and run states in both languages", () => {
    expect(stateLabel(de, "waiting")).toBe("wartet");
    expect(stateLabel(de, "done")).toBe("erledigt");
    expect(stateLabel(de, "failed")).toBe("fehlgeschlagen");
    expect(stateLabel(en, "waiting_for_input")).toBe("waiting for input");
    expect(stateLabel(en, "something_new")).toBe("something_new");
  });
});
