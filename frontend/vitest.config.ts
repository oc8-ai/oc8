// Minimal vitest config -- this project's `package.json` `test:unit` script
// has pointed at `vitest run --config vitest.config.ts` since 446abb6, but no
// such file was ever added alongside it (verified via `git log --all` for
// this path -- zero commits). Without it, `vitest` falls back to Node's
// default environment (no DOM), so every React Testing Library test in
// `src/test/*.test.tsx` fails at `render()` with "document is not defined".
// This file exists purely to make `bun run test:unit` runnable; it is not
// tied to any particular test file or feature.
import { defineConfig } from "vitest/config";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The app's version, from package.json, for the sidebar footer
// (src/lib/app-version.ts).
const { version } = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
) as { version: string };

export default defineConfig({
  define: { "import.meta.env.VITE_APP_VERSION": JSON.stringify(version) },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "@oc8/edition-entry": fileURLToPath(new URL("./src/edition/entry.ts", import.meta.url)),
    },
  },
  test: {
    // `e2e/*.spec.ts` are Playwright specs (see `test:e2e`), not vitest
    // ones -- without this they match vitest's default glob too and blow up
    // on `test.describe()` outside a Playwright config.
    exclude: ["e2e/**", "node_modules/**"],
    environment: "jsdom",
    // `@testing-library/react`'s automatic post-test `cleanup()` only
    // registers itself when it finds a global `afterEach` -- without
    // `globals: true` that hook never fires, so each test's render stays in
    // the jsdom `document` and the next test's `findByText`/`getByText`
    // sees duplicates from every prior test in the same file.
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
  },
});
