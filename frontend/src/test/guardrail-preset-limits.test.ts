import { describe, expect, it } from "vitest";
import {
  libraryPolicyOf,
  matches,
  policyOf,
  type GuardrailLibraryEntry,
  type GuardrailPreset,
} from "@/components/guardrail-preset-picker";

const capped: GuardrailPreset = {
  key: "autonomous_with_limit",
  label: "Autonomous, max 50 mails a day",
  labelTranslations: {},
  summary: "",
  summaryTranslations: {},
  recommended: false,
  read: true,
  modify: true,
  approvalActions: [],
  approvalEur: null,
  only: ["mail_get", "mail_send", "mail_reply"],
  limits: [{ tools: ["mail_send", "mail_reply"], max: 50, per: "day" }],
};

describe("presets with usage limits", () => {
  it("selecting a preset writes its limits", () => {
    expect(policyOf(capped).limits).toEqual(capped.limits);
  });

  it("a changed max or a removed limit no longer reads as the preset", () => {
    const applied = policyOf(capped);
    expect(matches(capped, applied, false)).toBe(true);
    expect(matches(capped, { ...applied, limits: [{ ...capped.limits[0], max: 20 }] }, false)).toBe(
      false,
    );
    expect(matches(capped, { ...applied, limits: [] }, false)).toBe(false);
  });

  it("a library entry's limits survive selection", () => {
    const entry = {
      ...capped,
      useCase: "mail",
      adjustable: [],
    } as unknown as GuardrailLibraryEntry;
    expect(libraryPolicyOf(entry).limits).toEqual(capped.limits);
  });
});
