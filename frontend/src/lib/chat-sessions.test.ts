import { describe, expect, it } from "vitest";

import { mostRecentSession } from "@/lib/chat-sessions";

describe("the default chat", () => {
  it("is the most recently active one, not a pinned one listed first", () => {
    const sessions = [
      { id: "pinned-old", lastMessageAt: "2026-01-01T00:00:00+00:00" },
      { id: "latest", lastMessageAt: "2026-03-01T00:00:00+00:00" },
      { id: "middle", lastMessageAt: "2026-02-01T00:00:00+00:00" },
    ];
    expect(mostRecentSession(sessions)?.id).toBe("latest");
  });

  it("ranks a chat that never had a message below one that did", () => {
    const sessions = [
      { id: "empty", lastMessageAt: null },
      { id: "used", lastMessageAt: "2026-01-01T00:00:00+00:00" },
    ];
    expect(mostRecentSession(sessions)?.id).toBe("used");
  });

  it("keeps list order on a tie and is undefined for no chats", () => {
    expect(
      mostRecentSession([
        { id: "a", lastMessageAt: null },
        { id: "b", lastMessageAt: null },
      ])?.id,
    ).toBe("a");
    expect(mostRecentSession([])).toBeUndefined();
    expect(mostRecentSession(undefined)).toBeUndefined();
  });
});
