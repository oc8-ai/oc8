import { describe, expect, it } from "vitest";

import { buildTurns } from "@/components/conversation/use-conversation";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

function msg(
  id: string,
  role: ChatMessageDTO["role"],
  extra: Partial<ChatMessageDTO> = {},
): ChatMessageDTO {
  return {
    id,
    sessionId: "s",
    role,
    content: id,
    runId: null,
    renderedComponents: [],
    createdAt: "",
    attachments: [],
    mode: null,
    contextRefs: [],
    authorMemberId: null,
    authorName: null,
    replyToId: null,
    ...extra,
  };
}

describe("buildTurns", () => {
  it("pairs each user turn with the answers that reply to it", () => {
    const turns = buildTurns(
      [
        msg("u1", "user"),
        msg("a1", "assistant", { replyToId: "u1", runId: "r1" }),
        msg("u2", "user"),
        msg("a2", "assistant", { replyToId: "u2", runId: "r2" }),
      ],
      [],
    );
    expect(turns.map((t) => t.kind)).toEqual(["user", "assistant", "user", "assistant"]);
  });

  it("keeps regenerated answers as variants of one turn", () => {
    const turns = buildTurns(
      [
        msg("u1", "user"),
        msg("a1", "assistant", { replyToId: "u1" }),
        msg("a2", "assistant", { replyToId: "u1" }),
      ],
      [],
    );
    expect(turns).toHaveLength(2);
    const answer = turns[1];
    expect(answer.kind === "assistant" && answer.variants.map((v) => v.id)).toEqual(["a1", "a2"]);
  });

  it("attaches replies by replyToId when two turns are pending", () => {
    const turns = buildTurns(
      [msg("u1", "user"), msg("u2", "user"), msg("a2", "assistant", { replyToId: "u2" })],
      [{ runId: "r1", replyToId: "u1", state: "running" }],
    );
    expect(
      turns.map((t) => [
        t.kind,
        t.kind === "assistant" ? t.variants.map((v) => v.id).join() || t.liveRunId : t.message.id,
      ]),
    ).toEqual([
      ["user", "u1"],
      ["assistant", "r1"],
      ["user", "u2"],
      ["assistant", "a2"],
    ]);
  });

  it("shows a running regeneration after the existing answer", () => {
    const turns = buildTurns(
      [msg("u1", "user"), msg("a1", "assistant", { replyToId: "u1" })],
      [{ runId: "r2", replyToId: "u1", state: "running" }],
    );
    const answer = turns[1];
    expect(answer.kind === "assistant" && answer.liveRunId).toBe("r2");
    expect(answer.kind === "assistant" && answer.variants).toHaveLength(1);
  });

  it("keeps legacy answers without a reply link in transcript order", () => {
    const turns = buildTurns([msg("u1", "user"), msg("a1", "assistant")], []);
    expect(turns.map((t) => t.kind)).toEqual(["user", "assistant"]);
  });

  it("keeps an answer whose user turn is missing from the list", () => {
    const turns = buildTurns(
      [
        msg("a0", "assistant", { replyToId: "gone" }),
        msg("u1", "user"),
        msg("a1", "assistant", { replyToId: "u1" }),
      ],
      [],
    );
    expect(
      turns.map((t) => (t.kind === "assistant" ? t.variants.map((v) => v.id).join() : t.key)),
    ).toEqual(["a0", "u1", "a1"]);
    const orphan = turns[0];
    expect(orphan.kind === "assistant" && orphan.replyToId).toBeNull();
  });

  it("keeps three regenerated answers as variants of one turn", () => {
    const turns = buildTurns(
      [
        msg("u1", "user"),
        msg("a1", "assistant", { replyToId: "u1" }),
        msg("a2", "assistant", { replyToId: "u1" }),
        msg("a3", "assistant", { replyToId: "u1" }),
      ],
      [],
    );
    expect(turns).toHaveLength(2);
    const answer = turns[1];
    expect(answer.kind === "assistant" && answer.variants.map((v) => v.id)).toEqual([
      "a1",
      "a2",
      "a3",
    ]);
  });

  it("pairs a follow-up turn with its answer like a user turn", () => {
    const turns = buildTurns(
      [
        msg("u1", "user"),
        msg("a1", "assistant", { replyToId: "u1" }),
        msg("f1", "followup"),
        msg("a2", "assistant", { replyToId: "f1" }),
      ],
      [],
    );
    expect(turns.map((t) => t.key)).toEqual(["u1", "answer-u1", "f1", "answer-f1"]);
  });
});
