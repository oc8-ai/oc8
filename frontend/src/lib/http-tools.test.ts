import { describe, expect, it } from "vitest";
import { PARAM_SCHEMA_MAX_BYTES, validateHttpTools } from "@/lib/http-tools";

// An object schema whose compact JSON is exactly `n` UTF-8 bytes, most of it
// a German description (two bytes per umlaut) -- the way the server measures.
function schemaOfBytes(n: number): string {
  const empty = new TextEncoder().encode(
    JSON.stringify({ type: "object", description: "" }),
  ).length;
  const room = n - empty;
  return JSON.stringify({
    type: "object",
    description: "ä".repeat(Math.floor(room / 2)) + "a".repeat(room % 2),
  });
}

describe("validateHttpTools", () => {
  it("counts a schema's size in UTF-8 bytes, like the server", () => {
    const tool = { name: "t1", urlTemplate: "/x" };
    expect(
      validateHttpTools([{ ...tool, paramSchema: schemaOfBytes(PARAM_SCHEMA_MAX_BYTES) }]),
    ).toEqual([null]);
    expect(
      validateHttpTools([{ ...tool, paramSchema: schemaOfBytes(PARAM_SCHEMA_MAX_BYTES + 1) }]),
    ).toEqual(["schema_size"]);
  });
});
