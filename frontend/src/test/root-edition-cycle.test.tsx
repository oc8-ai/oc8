import { describe, expect, it, vi } from "vitest";

// An edition's routes import the root route, and the edition entry imports
// those routes. When the server loads the edition entry first (for its server
// hooks), the root route module runs while the entry is still loading, so its
// exports are not there yet. The root route must not read them while it loads.

const entry = vi.hoisted(() => ({ loaded: false }));

vi.mock("@oc8/edition-entry", () => ({
  get editionExtensions() {
    return entry.loaded ? [] : undefined;
  },
  get editionRoutes() {
    return entry.loaded ? [] : undefined;
  },
  editionServer: {},
}));

describe("the root route while the edition entry is still loading", () => {
  it("loads without reading the edition's extensions", async () => {
    await expect(import("../routes/__root")).resolves.toHaveProperty("Route");
    entry.loaded = true;
  });
});
