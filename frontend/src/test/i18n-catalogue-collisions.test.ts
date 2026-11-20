// frontend/src/test/i18n-catalogue-collisions.test.ts
// A `de.po` entry wins over a call's inline German (`useT`). When a short
// English msgid already exists with another meaning ("Sources" = data
// sources, "Run" = the verb), the inline German never shows: the chat must
// use a distinct msgid instead (DESIGN.md, Copy). This fails on any `t(en,
// de)` pair in the chat whose catalogue entry says something else.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(process.cwd(), "src");
const PO = join(process.cwd(), "..", "i18n", "de.po");

/** The chat module and the files its surfaces are made of. */
const CHAT = [
  "components/conversation",
  "lib/conversation-api.ts",
  "lib/hooks-chat.ts",
  "lib/chat-commands.ts",
  "lib/chat-suggestions.ts",
  "lib/run-state.ts",
  "lib/tool-labels.ts",
  "routes/copilot.tsx",
  "routes/activity.tsx",
  "components/copilot-dock.tsx",
  "components/dashboard/widgets/chat-widget.tsx",
];

function sources(rel: string): string[] {
  const full = join(SRC, rel);
  if (!statSync(full).isDirectory()) return [full];
  return readdirSync(full).flatMap((entry) => sources(join(rel, entry)));
}

function catalogue(): Map<string, string> {
  const entries = new Map<string, string>();
  let id: string | null = null;
  let str: string | null = null;
  let mode: "id" | "str" | null = null;
  const flush = () => {
    if (id && str !== null) entries.set(id, str);
  };
  for (const line of readFileSync(PO, "utf8").split("\n")) {
    if (line.startsWith("msgid ")) {
      flush();
      id = JSON.parse(line.slice(6)) as string;
      str = null;
      mode = "id";
    } else if (line.startsWith("msgstr ")) {
      str = JSON.parse(line.slice(7)) as string;
      mode = "str";
    } else if (line.startsWith('"')) {
      const more = JSON.parse(line) as string;
      if (mode === "id") id += more;
      else if (mode === "str") str += more;
    }
  }
  flush();
  return entries;
}

const STRING = String.raw`"((?:[^"\\]|\\.)*)"`;
const PAIR = new RegExp(String.raw`\bt\(\s*${STRING}\s*,\s*${STRING}\s*[,)]`, "g");

describe("the chat's German is never overridden by another meaning", () => {
  const po = catalogue();
  it.each(CHAT.flatMap(sources).filter((f) => /\.tsx?$/.test(f)))("%s", (file) => {
    const source = readFileSync(file, "utf8");
    const clashes: string[] = [];
    for (const m of source.matchAll(PAIR)) {
      const en = JSON.parse(`"${m[1]}"`) as string;
      const de = JSON.parse(`"${m[2]}"`) as string;
      const listed = po.get(en);
      if (listed !== undefined && listed !== de)
        clashes.push(`${en}: "${de}" vs de.po "${listed}"`);
    }
    expect(clashes).toEqual([]);
  });

  it("calls a knowledge base a Wissensdatenbank everywhere", () => {
    const everywhere = [
      readFileSync(PO, "utf8"),
      ...sources("")
        .filter((f) => /\.tsx?$/.test(f) && !f.includes("/test/"))
        .map((f) => readFileSync(f, "utf8")),
    ];
    for (const text of everywhere) expect(text).not.toMatch(/Wissensbas(is|en)/);
  });
});
