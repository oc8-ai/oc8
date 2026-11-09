import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { APP_VERSION } from "@/lib/app-version";

describe("the app version (E2E L)", () => {
  it("is package.json's, set at build time", () => {
    const pkg = JSON.parse(readFileSync(join(process.cwd(), "package.json"), "utf8")) as {
      version: string;
    };
    expect(APP_VERSION).toBe(pkg.version);
  });

  it("is what the sidebar footer shows, at a type-scale size", () => {
    const shell = readFileSync(join(process.cwd(), "src/components/app-shell.tsx"), "utf8");
    expect(shell).not.toMatch(/v0\.9/);
    expect(shell).toMatch(/<span className="font-mono text-xs">v\{APP_VERSION\}<\/span>/);
  });
});
