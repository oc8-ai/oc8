import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// The connection notice links through the router (no full page reload). Stub
// its Link as an anchor that renders the href it would navigate to.
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({
      children,
      to,
      params,
      search,
    }: {
      children?: React.ReactNode;
      to: string;
      params?: Record<string, string>;
      search?: Record<string, string>;
    }) => {
      const path = to.replace(/\$(\w+)/g, (_, key: string) => params?.[key] ?? "");
      const query = search ? `?${new URLSearchParams(search).toString()}` : "";
      return <a href={`${path}${query}`}>{children}</a>;
    },
  };
});
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
    expect(screen.getByTestId("stream-tail")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Copy answer/ })).toBeNull();
  });

  it("offers to connect the account a tool call needed", () => {
    wrap(
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [], liveRunId: "r1" }}
        run={{
          id: "r1",
          agentId: "a1",
          state: "done",
          phase: null,
          output: "done",
          steps: 1,
          toolCalls: [],
          taskId: null,
          question: null,
          renderedComponents: [],
          connectionRequired: {
            integration: "microsoft365",
            provider: "microsoft",
            label: "Microsoft 365",
            reason: "not_connected",
          },
        }}
      />,
    );
    expect(
      screen.getByText("For this task the agent needs your Microsoft account."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect now →" })).toHaveAttribute(
      "href",
      "/connections?connect=microsoft365",
    );
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

describe("turn view fixes", () => {
  const a1 = { ...base, id: "a1", content: "First answer" };
  const a2 = { ...base, id: "a2", content: "Second answer" };

  it("follows a new answer after Regenerate even when paged to an older one", async () => {
    const { rerender } = wrap(
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [a1, a2], liveRunId: null }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Previous answer/ }));
    expect(screen.getByText("1/2")).toBeInTheDocument();
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <AssistantTurn
          sessionId="s"
          turn={{
            kind: "assistant",
            key: "k",
            replyToId: "u1",
            variants: [a1, a2],
            liveRunId: "r2",
          }}
          run={{
            id: "r2",
            agentId: "a",
            state: "running",
            phase: null,
            output: null,
            steps: 1,
            toolCalls: [],
            taskId: null,
            question: null,
            renderedComponents: [],
            liveAnswer: "Fresh take",
          }}
        />
      </QueryClientProvider>,
    );
    // While streaming, the newest words are their own fading span and are
    // revealed at a steady pace: match the paragraph once it is all there.
    expect(
      await screen.findByText((_, el) => el?.tagName === "P" && el.textContent === "Fresh take"),
    ).toBeInTheDocument();
  });

  it("keeps the rating per answer and resets it on a variant switch", () => {
    wrap(
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [a1, a2], liveRunId: null }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Good answer/ }));
    expect(screen.getByRole("button", { name: /Good answer/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: /Previous answer/ }));
    expect(screen.getByRole("button", { name: /Good answer/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("does not throw when the clipboard is unavailable", () => {
    const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    try {
      wrap(
        <AssistantTurn
          sessionId="s"
          turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [a1], liveRunId: null }}
        />,
      );
      const errors: unknown[] = [];
      const onError = (e: ErrorEvent) => {
        errors.push(e.error);
        e.preventDefault();
      };
      window.addEventListener("error", onError);
      fireEvent.click(screen.getByRole("button", { name: /Copy answer/ }));
      window.removeEventListener("error", onError);
      expect(errors).toEqual([]);
    } finally {
      if (original) Object.defineProperty(navigator, "clipboard", original);
    }
  });

  it("gives the actions a 36px hit area", () => {
    wrap(
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [a1], liveRunId: null }}
      />,
    );
    expect(screen.getByRole("button", { name: /Copy answer/ })).toHaveClass("size-9");
  });

  it("shows Edit and resend on touch screens with a large hit area", () => {
    const message = { ...base, id: "u1", role: "user" as const, content: "typo" };
    wrap(
      <UserTurn
        turn={{ kind: "user", key: "u1", message }}
        viewerMemberId={null}
        canEdit
        onEdit={vi.fn()}
      />,
    );
    const edit = screen.getByRole("button", { name: /Edit and resend/ });
    expect(edit).toHaveClass(
      "[@media(hover:none)]:opacity-100",
      "min-h-9",
      "[@media(pointer:coarse)]:min-h-11",
    );
  });
});
