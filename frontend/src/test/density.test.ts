// The app's default density: a 90 % root font on desktop (what the owner
// found comfortable at 90 % browser zoom), 100 % on phones and touch screens.
// A source scan, because jsdom applies neither media queries nor rem maths.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(process.cwd(), "src", "styles.css"), "utf8");

describe("root density", () => {
  it("scales the root by percentage, only for a precise pointer on a wide screen", () => {
    expect(css).toMatch(/html\s*\{\s*font-size:\s*100%;/);
    expect(css).toMatch(
      /@media \(min-width: 48rem\) and \(pointer: fine\)\s*\{\s*html\s*\{\s*font-size:\s*90%;/,
    );
    // Never a fixed px root (would override the reader's own font setting)
    // and never zoom/transform scaling (breaks pointer maths and poppers).
    expect(css).not.toMatch(/html\s*\{[^}]*font-size:\s*\d+px/);
    expect(css).not.toMatch(/^\s*zoom\s*:/m);
  });

  it("keeps the smallest type step readable at the denser root", () => {
    expect(css).toMatch(/--text-xs:\s*max\(0\.75rem,\s*11\.5px\)/);
  });
});
