import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));

import { AssistantTurn } from "@/components/conversation/assistant-turn";
import type { AssistantTurnModel, UserTurnModel } from "@/components/conversation/types";
import { UserTurn } from "@/components/conversation/user-turn";
import { buildTurns } from "@/components/conversation/use-conversation";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

function msg(
  id: string,
  role: ChatMessageDTO["role"],
  parentId: string | null,
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
    authorMemberId: role === "user" ? "me" : null,
    authorName: null,
    replyToId: role === "assistant" ? parentId : null,
    parentId,
    ...extra,
  };
}

// u1 -> a1 -> u2 -> a2   and an edited branch   u1 -> a1 -> u2b -> a2b
// plus a regenerated answer a1b next to a1 (both children of u1).
const tree = [
  msg("u1", "user", null),
  msg("a1", "assistant", "u1"),
  msg("a1b", "assistant", "u1"),
  msg("u2", "user", "a1"),
  msg("a2", "assistant", "u2"),
  msg("u2b", "user", "a1"),
  msg("a2b", "assistant", "u2b"),
];

describe("buildTurns with a current leaf", () => {
  it("renders only the path from the root to the leaf", () => {
    const turns = buildTurns(tree, [], "a2b");
    const text = turns.map((t) =>
      t.kind === "user" ? t.message.id : t.variants.map((v) => v.id).join("|"),
    );
    expect(text).toEqual(["u1", "a1|a1b", "u2b", "a2b"]);
  });

  it("marks the path's answer as the selected variant and lists user siblings", () => {
    const turns = buildTurns(tree, [], "a2b");
    const answer = turns[1] as AssistantTurnModel;
    expect(answer.selectedId).toBe("a1");
    const edited = turns[2] as UserTurnModel;
    expect(edited.siblings).toEqual({ ids: ["u2", "u2b"], index: 1 });
    expect((turns[0] as UserTurnModel).siblings).toEqual({ ids: ["u1"], index: 0 });
  });

  it("follows the regenerated variant when the leaf is below it", () => {
    const turns = buildTurns(
      [msg("u1", "user", null), msg("a1", "assistant", "u1"), msg("a1b", "assistant", "u1")],
      [],
      "a1b",
    );
    expect((turns[1] as AssistantTurnModel).selectedId).toBe("a1b");
  });

  it("renders everything as before when the leaf is null", () => {
    const turns = buildTurns(
      [msg("u1", "user", null), msg("a1", "assistant", "u1"), msg("u2", "user", "a1")],
      [],
      null,
    );
    expect(turns.map((t) => t.kind)).toEqual(["user", "assistant", "user"]);
    expect((turns[1] as AssistantTurnModel).selectedId).toBeUndefined();
  });

  it("hides a live run that belongs to another branch", () => {
    const turns = buildTurns(tree, [{ runId: "r9", replyToId: "u2", state: "running" }], "a2b");
    expect(turns.some((t) => t.kind === "assistant" && t.liveRunId === "r9")).toBe(false);
  });
});

function wrap(ui: React.ReactElement) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

const userTurn: UserTurnModel = {
  kind: "user",
  key: "u2b",
  message: msg("u2b", "user", "a1", { content: "typo" }),
  siblings: { ids: ["u2", "u2b", "u2c"], index: 1 },
};

describe("UserTurn in a tree", () => {
  it("switches to the neighbouring sibling with ‹ 2/3 ›", () => {
    const onSwitch = vi.fn();
    wrap(
      <UserTurn
        turn={userTurn}
        viewerMemberId="me"
        canEdit={false}
        onEdit={vi.fn()}
        onSwitch={onSwitch}
      />,
    );
    expect(screen.getByText("2/3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Previous message/ }));
    expect(onSwitch).toHaveBeenCalledWith("u2");
    fireEvent.click(screen.getByRole("button", { name: /Next message/ }));
    expect(onSwitch).toHaveBeenCalledWith("u2c");
  });

  it("saves an inline edit and restores the bubble on cancel", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    wrap(<UserTurn turn={userTurn} viewerMemberId="me" canEdit onEdit={vi.fn()} onSave={onSave} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByRole("textbox");
    expect(box).toHaveValue("typo");
    fireEvent.change(box, { target: { value: "fixed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("fixed"));

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "other" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText("typo")).toBeInTheDocument();
  });

  it("hints that a leading slash is sent literally", () => {
    wrap(
      <UserTurn turn={userTurn} viewerMemberId="me" canEdit onEdit={vi.fn()} onSave={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.queryByText(/sent as written/)).toBeNull();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "/plan x" } });
    expect(screen.getByText(/sent as written/)).toBeInTheDocument();
  });
});

describe("AssistantTurn in a tree", () => {
  it("switching a variant sets the leaf instead of only paging locally", () => {
    const a1 = msg("a1", "assistant", "u1", { content: "First" });
    const a1b = msg("a1b", "assistant", "u1", { content: "Second" });
    const onSelectVariant = vi.fn();
    wrap(
      <AssistantTurn
        sessionId="s"
        turn={{
          kind: "assistant",
          key: "k",
          replyToId: "u1",
          variants: [a1, a1b],
          selectedId: "a1",
          liveRunId: null,
        }}
        onSelectVariant={onSelectVariant}
      />,
    );
    expect(screen.getByText("First")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Next answer/ }));
    expect(onSelectVariant).toHaveBeenCalledWith("a1b");
  });
});
