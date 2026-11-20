import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));

import { AssistantTurn } from "@/components/conversation/assistant-turn";
import { isEmptyReply } from "@/components/conversation/empty-reply";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

const empty: ChatMessageDTO = {
  id: "a1",
  sessionId: "s",
  role: "assistant",
  content: "  ",
  runId: null,
  renderedComponents: [],
  createdAt: "2026-11-10T09:00:00Z",
  attachments: [],
  mode: null,
  contextRefs: [],
  authorMemberId: null,
  authorName: null,
  replyToId: "u1",
};

const NO_ANSWER = "The assistant returned no answer. Try again or rephrase.";

function renderTurn(message: ChatMessageDTO, onRegenerate = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AssistantTurn
        sessionId="s"
        turn={{
          kind: "assistant",
          key: "a",
          replyToId: "u1",
          variants: [message],
          liveRunId: null,
        }}
        onRegenerate={onRegenerate}
      />
    </QueryClientProvider>,
  );
  return onRegenerate;
}

describe("an empty assistant reply", () => {
  it("says the turn ended without an answer and offers to regenerate", () => {
    const onRegenerate = renderTurn(empty);
    expect(screen.getByRole("status")).toHaveTextContent(NO_ANSWER);
    fireEvent.click(screen.getByRole("button", { name: "Regenerate answer" }));
    expect(onRegenerate).toHaveBeenCalledWith("u1");
  });

  it("is not empty when it only carries an agent_builder card", () => {
    const withCard = { ...empty, parts: [{ type: "agent_builder", proposalId: "p1" }] };
    expect(isEmptyReply(withCard)).toBe(false);
    renderTurn(withCard);
    expect(screen.queryByText(NO_ANSWER)).not.toBeInTheDocument();
  });

  it("is not empty when it carries answered questions, suggestions or components", () => {
    expect(isEmptyReply({ ...empty, parts: [{ type: "answers", items: [] }] })).toBe(false);
    expect(isEmptyReply({ ...empty, suggestions: ["Weiter"] })).toBe(false);
    expect(
      isEmptyReply({
        ...empty,
        renderedComponents: [
          { type: "table" } as unknown as ChatMessageDTO["renderedComponents"][number],
        ],
      }),
    ).toBe(false);
    expect(isEmptyReply({ ...empty, content: "Fertig." })).toBe(false);
    expect(isEmptyReply(null)).toBe(false);
  });
});
