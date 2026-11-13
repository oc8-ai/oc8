// Only a saved answer's links are checked, so only they can be clickable: the
// live text keeps its links as text -- even once the stream has ended -- until
// the saved message replaces it. A member's own message is not checked.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));
// The server's verdicts: no link here points at a connected system.
const asked = vi.hoisted(() => vi.fn());
vi.mock("@/lib/record-links", async (orig) => ({
  ...(await orig<typeof import("@/lib/record-links")>()),
  useVerifiedLinks: (messageId: string | undefined, urls: string[]) => {
    if (messageId && urls.length) asked(messageId);
    return {
      data: messageId
        ? new Map(
            urls.map((url) => [
              url,
              { url, host: new URL(url).hostname, connected: false, issued: false },
            ]),
          )
        : undefined,
    };
  },
}));

import { AssistantTurn } from "@/components/conversation/assistant-turn";
import { UserTurn } from "@/components/conversation/user-turn";
import type { ChatMessageDTO, RunActivityDTO } from "@/lib/hooks-chat";

const ANSWER = "See [the docs](https://example.com/docs).";

const run: RunActivityDTO = {
  id: "r1",
  agentId: "a",
  state: "done",
  phase: null,
  output: null,
  steps: 0,
  toolCalls: [],
  taskId: null,
  question: null,
  renderedComponents: [],
  liveAnswer: ANSWER,
};

const saved: ChatMessageDTO = {
  id: "m1",
  sessionId: "s",
  role: "assistant",
  content: ANSWER,
  runId: "r1",
  renderedComponents: [],
  createdAt: "2026-11-13T09:00:00Z",
  attachments: [],
  mode: null,
  contextRefs: [],
  authorMemberId: null,
  authorName: null,
  replyToId: "u1",
};

function renderTurn(variants: ChatMessageDTO[], liveRunId: string | null) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AssistantTurn
        sessionId="s"
        run={run}
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants, liveRunId }}
      />
    </QueryClientProvider>,
  );
}

describe("links in a live answer", () => {
  it("stay text after the stream ended, while no saved message exists", () => {
    renderTurn([], "r1");
    expect(screen.getByText("the docs")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /the docs/ })).toBeNull();
  });

  it("become links once the saved message replaces the live text", () => {
    renderTurn([saved], null);
    expect(screen.getByRole("link", { name: /the docs/ })).toHaveAttribute(
      "href",
      "https://example.com/docs",
    );
    expect(asked).toHaveBeenCalledWith("m1");
  });
});

describe("links in a member's own message", () => {
  it("are clickable as written, without a check", () => {
    asked.mockClear();
    const url = "https://odoo.example.com/odoo/sale.order/99";
    render(
      <QueryClientProvider client={new QueryClient()}>
        <UserTurn
          turn={{
            kind: "user",
            key: "u1",
            message: {
              ...saved,
              id: "u1",
              role: "user",
              content: `[my order](${url})`,
              authorMemberId: "me",
            },
          }}
          viewerMemberId={null}
          canEdit={false}
          onEdit={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("link", { name: /my order/ })).toHaveAttribute("href", url);
    expect(asked).not.toHaveBeenCalled();
  });
});
