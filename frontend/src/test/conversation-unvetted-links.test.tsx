// A streamed answer never went through the server's link check; the message
// saved from it did. So the live text keeps its links as text -- even once
// the stream has ended -- until the saved message replaces it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/lib/record-links", () => ({
  useConnectedHosts: () => ({ data: new Set(["odoo.example.com"]) }),
  useVerifiedLinks: () => ({ data: new Set<string>() }),
}));

import { AssistantTurn } from "@/components/conversation/assistant-turn";
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
  });
});
