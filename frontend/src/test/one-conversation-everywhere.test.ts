// frontend/src/test/one-conversation-everywhere.test.ts
// Spec §8 invariant 1: all surfaces render the same Conversation. A surface
// that mounts its own transcript or textarea fails here.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(process.cwd(), "src");
const SURFACES = [
  "routes/copilot.tsx",
  "components/copilot-dock.tsx",
  "components/dashboard/widgets/chat-widget.tsx",
  "components/conversation/live-timeline.tsx",
];

describe("one conversation everywhere", () => {
  it.each(SURFACES)("%s mounts Conversation and no list or composer of its own", (rel) => {
    const source = readFileSync(join(SRC, rel), "utf8");
    expect(source).toMatch(/<Conversation\b/);
    expect(source).not.toMatch(/<textarea\b/);
    expect(source).not.toMatch(/useChatMessages\(/);
    expect(source).not.toMatch(/RUN_COMPONENT_REGISTRY/);
    expect(source).not.toMatch(/\bChatWindow\b|\bCopilotChatTab\b/);
  });

  // The agent page's Live tab is the Live timeline's Conversation. (Its
  // operator Live Log legitimately renders run detail, so the page itself
  // is not held to the rules above.)
  it("routes/agents.$id.tsx mounts the Live timeline, not the old chat window", () => {
    const source = readFileSync(join(SRC, "routes/agents.$id.tsx"), "utf8");
    expect(source).toMatch(/import \{ LiveTimeline \}/);
    expect(source).toMatch(/<LiveTimeline\b/);
    expect(source).not.toMatch(/\bChatWindow\b/);
  });
});

function allSources(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) allSources(full, acc);
    else if (/\.tsx?$/.test(entry)) acc.push(full);
  }
  return acc;
}

describe("the old chat is gone", () => {
  it("nothing references the retired chat components", () => {
    const offenders = allSources(SRC).filter((path) =>
      /\b(ChatWindow|CopilotChatTab|CopilotRunActivity|CopilotStreamingAnswer|useCopilotRunActivity|ChatSessionPicker)\b/.test(
        readFileSync(path, "utf8"),
      ),
    );
    expect(offenders.map((p) => p.slice(SRC.length + 1))).toEqual([
      "test/one-conversation-everywhere.test.ts",
    ]);
  });
});
