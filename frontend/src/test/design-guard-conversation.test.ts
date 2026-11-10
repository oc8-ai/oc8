// frontend/src/test/design-guard-conversation.test.ts
// Spec invariant 13: no new arbitrary text sizes, raw colours or spinners for
// content loading in the chat. A source scan, like
// steps-never-in-the-message-stream.test.ts, because the defect is a NEW line
// in a NEW file that no render test would look at.
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(process.cwd(), "src", "components", "conversation");
const STYLES = join(process.cwd(), "src", "styles.css");

function files(dir: string, acc: string[] = []): string[] {
  if (!existsSync(dir)) return acc;
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) files(full, acc);
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) acc.push(full);
  }
  return acc;
}

const RULES: Array<[string, RegExp]> = [
  ["arbitrary text size", /text-\[\d+(px|rem|em)\]/],
  [
    "raw palette colour",
    /\b(?:bg|text|border|ring|from|to|via)-(?:amber|emerald|blue|red|green|yellow|orange|sky|slate|gray|zinc)-\d{2,3}\b/,
  ],
  ["hard-coded light/dark", /\b(?:bg-white|text-black|bg-black|text-white)\b/],
  ["hex colour in a component", /#[0-9a-fA-F]{6}\b/],
  ["spinner for loading", /animate-spin/],
  ["bare rounded", /(?<![\w-])rounded(?![\w-])/],
  ["arbitrary padding", /\bp[xytrbl]?-\[\d/],
];

describe("DESIGN.md guard for components/conversation", () => {
  it("has no forbidden classes", () => {
    const offenders: string[] = [];
    for (const path of files(ROOT)) {
      const source = readFileSync(path, "utf8");
      for (const [name, rule] of RULES) {
        if (rule.test(source)) offenders.push(`${path.slice(ROOT.length + 1)}: ${name}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("declares the chat utilities and one global reduced-motion rule", () => {
    const css = readFileSync(STYLES, "utf8");
    for (const utility of [
      "text-micro",
      "turn-in",
      "text-shimmer",
      "stream-tail",
      "presence-mark",
    ]) {
      expect(css).toContain(`@utility ${utility}`);
    }
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{\s*\*,/);
  });
});
