import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));

import { AssistantTurn } from "@/components/conversation/assistant-turn";
import { UserTurn } from "@/components/conversation/user-turn";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

const base: ChatMessageDTO = {
  id: "m",
  sessionId: "s",
  role: "assistant",
  content: "",
  runId: null,
  renderedComponents: [],
  createdAt: "2026-11-08T09:00:00Z",
  attachments: [],
  mode: null,
  contextRefs: [],
  authorMemberId: null,
  authorName: null,
  replyToId: "u1",
};

function wrap(ui: React.ReactElement) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

describe("UserTurn", () => {
  it("shows mode, context chips and the author only when it is not the viewer", () => {
    const message = {
      ...base,
      id: "u1",
      role: "user" as const,
      content: "Plan the rollout",
      mode: "plan",
      contextRefs: [{ kind: "knowledge_base", id: "kb", label: "Handbook" }],
      authorMemberId: "other",
      authorName: "Ada",
    };
    const { rerender } = wrap(
      <UserTurn
        turn={{ kind: "user", key: "u1", message }}
        viewerMemberId="me"
        canEdit={false}
        onEdit={vi.fn()}
      />,
    );
    expect(screen.getByText("/plan")).toBeInTheDocument();
    expect(screen.getByText("Handbook")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <UserTurn
          turn={{ kind: "user", key: "u1", message }}
          viewerMemberId="other"
          canEdit={false}
          onEdit={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(screen.queryByText("Ada")).toBeNull();
  });

  it("offers Edit and resend only when allowed", () => {
    const onEdit = vi.fn();
    const message = { ...base, id: "u1", role: "user" as const, content: "typo here" };
    wrap(
      <UserTurn
        turn={{ kind: "user", key: "u1", message }}
        viewerMemberId={null}
        canEdit
        onEdit={onEdit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Edit and resend/ }));
    expect(onEdit).toHaveBeenCalledWith("typo here");
  });
});

describe("AssistantTurn", () => {
  it("switches between regenerated answers with ‹ 1/2 ›", () => {
    const a1 = { ...base, id: "a1", content: "First answer" };
    const a2 = { ...base, id: "a2", content: "Second answer" };
    wrap(
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [a1, a2], liveRunId: null }}
      />,
    );
    expect(screen.getByText("Second answer")).toBeInTheDocument();
    expect(screen.getByText("2/2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Previous answer/ }));
    expect(screen.getByText("First answer")).toBeInTheDocument();
  });

  it("streams the live run's answer and offers no actions until done", () => {
    wrap(
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [], liveRunId: "r1" }}
        run={{
          id: "r1",
          agentId: "a",
          state: "running",
          phase: null,
          output: null,
          steps: 1,
          toolCalls: [],
          taskId: null,
          question: null,
          renderedComponents: [],
          liveAnswer: "Partial",
        }}
      />,
    );
    expect(screen.getByText("Partial")).toBeInTheDocument();
    expect(screen.getByTestId("stream-caret")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Copy answer/ })).toBeNull();
  });

  it("renders sources and the cards slot", () => {
    const a1 = {
      ...base,
      id: "a1",
      content: "See [1].",
      parts: [
        {
          type: "sources",
          items: [
            {
              n: 1,
              title: "VAT",
              url: "https://gov.test/vat",
              favicon: null,
              snippet: "",
              kind: "web",
            },
          ],
        },
      ],
    };
    wrap(
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [a1], liveRunId: null }}
        cards={<div>approval card</div>}
      />,
    );
    expect(screen.getByText("Sources (1)")).toBeInTheDocument();
    expect(screen.getByText("approval card")).toBeInTheDocument();
  });
});
