import { describe, expect, it } from "vitest";

import { isUuid, urlId } from "@/lib/ids";

describe("ids from the address bar", () => {
  const id = "6F1C2B9E-3d4a-4c5b-9e8f-0a1b2c3d4e5f";
  it("accepts only a well-formed UUID", () => {
    expect(isUuid(id)).toBe(true);
    for (const bad of [
      "",
      "r1",
      "..",
      "../../members/x",
      `${id}/x`,
      ` ${id}`,
      `${id}%2F`,
      4,
      null,
    ]) {
      expect(isUuid(bad)).toBe(false);
    }
  });

  it("drops anything else", () => {
    expect(urlId(id)).toBe(id);
    expect(urlId("../x")).toBeUndefined();
  });
});
