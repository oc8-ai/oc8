// frontend/src/test/reasoning-levels.test.ts
import { describe, expect, it } from "vitest";

import type { ChatModelOptionDTO } from "@/lib/conversation-api";
import { effectiveOption } from "@/lib/reasoning-levels";

const AUTO: ChatModelOptionDTO = {
  id: null,
  label: "Auto",
  provider: "",
  locality: "",
  isDefault: false,
  supportsReasoning: true,
  defaultEffort: "balanced",
};
const LLAMA: ChatModelOptionDTO = {
  id: "m2",
  label: "Llama",
  provider: "ollama",
  locality: "local",
  isDefault: false,
  supportsReasoning: false,
  defaultEffort: "balanced",
};
const OPTIONS = [AUTO, LLAMA];

describe("effectiveOption", () => {
  it("is Auto while the chat has no model pick", () => {
    expect(effectiveOption(OPTIONS, null)).toBe(AUTO);
  });

  it("is the picked model when the chat has one", () => {
    expect(effectiveOption(OPTIONS, "m2")).toBe(LLAMA);
  });

  it("falls back to Auto for a pick that is not among the options", () => {
    expect(effectiveOption(OPTIONS, "gone")).toBe(AUTO);
  });

  it("is nothing when there are no options", () => {
    expect(effectiveOption([], "m2")).toBeUndefined();
  });
});
