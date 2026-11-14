import { describe, expect, it } from "vitest";
import {
  PARAM_SCHEMA_MAX_BYTES,
  customServerDraftProblem,
  validateHttpTools,
  type CustomServerDraft,
} from "@/lib/http-tools";

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

describe("customServerDraftProblem scope messages", () => {
  const tool = {
    name: "list_pets",
    description: "",
    method: "GET" as const,
    urlTemplate: "/pets",
    paramSchema: { type: "object", properties: {} },
  };
  function draft(scopes: { read: string[]; modify: string[] }): CustomServerDraft {
    return {
      baseUrl: "https://api.example.com",
      login: { kind: "none" } as CustomServerDraft["login"],
      httpTools: [tool],
      scopes,
      openapi: null,
    };
  }

  // A translator that only knows fixed sentences: an interpolated msgid
  // would miss the catalogue, which is what this guards against.
  function run(scopes: { read: string[]; modify: string[] }) {
    const msgids: string[] = [];
    const t = (en: string) => {
      msgids.push(en);
      return `[${en}]`;
    };
    const out = customServerDraftProblem(draft(scopes), t, { withConnection: false });
    return { out, msgids };
  }

  it("translates a fixed sentence for unknown endpoints and appends the names", () => {
    const { out, msgids } = run({ read: ["list_pets", "gone", "also_gone"], modify: [] });
    expect(msgids.join("|")).not.toContain("gone");
    expect(out).toMatch(/^\[.*\]/);
    expect(out).toContain("also_gone, gone");
  });

  it("translates a fixed sentence for endpoints in both lists and appends the names", () => {
    const { out, msgids } = run({ read: ["list_pets"], modify: ["list_pets"] });
    expect(msgids.join("|")).not.toContain("list_pets");
    expect(out).toContain("list_pets");
  });
});
