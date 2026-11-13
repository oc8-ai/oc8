import { describe, expect, it } from "vitest";
import { mergeLocales } from "@/lib/i18n";

describe("mergeLocales", () => {
  it("lists built-ins plus only locales that carry strings", () => {
    const out = mergeLocales([
      { locale: "fr", nativeName: "Français", flag: "🇫🇷", translations: { Save: "Enregistrer" } },
      { locale: "es", nativeName: "Español", flag: "🇪🇸", translations: {} },
    ]);
    expect(out.map((l) => l.code)).toEqual(["en", "de", "fr"]);
  });
});
