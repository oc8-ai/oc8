// Browser checks that jsdom cannot make (`npm run test:e2e`). They serve their
// own pages; no app or backend needs to run.
import { tmpdir } from "node:os";
import { join } from "node:path";

import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  outputDir: join(tmpdir(), "oc8-playwright"),
  reporter: "list",
  use: { browserName: "chromium", headless: true },
});
