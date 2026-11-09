// frontend/src/test/conversation-decisions-suggestions.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const decide = vi.hoisted(() => vi.fn());
const approvals = vi.hoisted(() => ({ current: [] as unknown[] }));
const me = vi.hoisted(() => ({ current: { memberId: "me" } as Record<string, unknown> }));
const conv = vi.hoisted(() => ({
  current: {
    session: undefined as unknown,
    messages: [] as unknown[],
    turns: [] as unknown[],
    liveRunIds: [] as string[],
  },
}));
vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useDecideApproval: () => ({ mutate: decide, isPending: false }),
  useApprovals: () => ({ data: approvals.current }),
  useAuth: () => ({ data: me.current }),
  useBudgetStatus: () => ({ data: undefined }),
}));
vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({ ...conv.current, isLoading: false, addPendingRun: vi.fn() }),
}));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({ data: [] }),
}));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
  useMay: () => () => true,
}));

import { Conversation } from "@/components/conversation";
import { DecisionCard } from "@/components/conversation/decision-card";
import { PromptStarters } from "@/components/conversation/prompt-starters";
import { SuggestionChips } from "@/components/conversation/suggestion-chips";
import { ApiError, api } from "@/lib/api";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

describe("DecisionCard", () => {
  it("marks the recommendation and decides through the approval", () => {
    render(
      <DecisionCard
        approval={
          {
            id: "ap1",
            agentId: "a",
            title: "Refund 40 €?",
            detail: "Order 4711 arrived broken.",
            amount: null,
            time: "",
            status: "pending",
            actionType: "decision",
            recommendation: "refund",
            options: [
              { key: "refund", label: "Refund", detail: "" },
              { key: "replace", label: "Send a replacement", detail: "" },
            ],
          } as never
        }
      />,
    );
    expect(screen.getByText("Recommended")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Decide: Send a replacement" }));
    expect(decide).toHaveBeenCalledWith(
      { approvalId: "ap1", decision: "approve", option: "replace" },
      expect.anything(),
    );
  });
});

describe("SuggestionChips", () => {
  it("sends on click and inserts with the pencil", () => {
    const onSend = vi.fn();
    const onInsert = vi.fn();
    render(
      <SuggestionChips
        suggestions={[
          { label: "Shorter", text: "Shorter" },
          { label: "Send it", text: "Send it" },
        ]}
        onSend={onSend}
        onInsert={onInsert}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Shorter" }));
    expect(onSend).toHaveBeenCalledWith("Shorter");
    fireEvent.click(screen.getByRole("button", { name: "Edit before sending: Send it" }));
    expect(onInsert).toHaveBeenCalledWith("Send it");
  });

  it("shows the label and sends or inserts the text", () => {
    const onSend = vi.fn();
    const onInsert = vi.fn();
    render(
      <SuggestionChips
        suggestions={[{ label: "Summarise this", text: "/summarise this conversation" }]}
        onSend={onSend}
        onInsert={onInsert}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Summarise this" }));
    expect(onSend).toHaveBeenCalledWith("/summarise this conversation");
    fireEvent.click(screen.getByRole("button", { name: "Edit before sending: Summarise this" }));
    expect(onInsert).toHaveBeenCalledWith("/summarise this conversation");
    expect(screen.queryByText("/summarise this conversation")).toBeNull();
  });
});

describe("PromptStarters", () => {
  it("shows title and subtitle cards", () => {
    const onPick = vi.fn();
    render(
      <PromptStarters
        starters={[{ title: "What is waiting on me?", subtitle: "Approvals and questions" }]}
        onPick={onPick}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /What is waiting on me\?/ }));
    expect(onPick).toHaveBeenCalledWith("What is waiting on me?");
    expect(screen.getByText("Approvals and questions")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------ edge cases

const decision = {
  id: "ap1",
  agentId: "a",
  title: "Refund 40 €?",
  detail: "",
  amount: null,
  time: "",
  status: "pending",
  actionType: "decision",
  recommendation: "refund",
  departmentId: "d1",
  taskId: "task-1",
  options: [
    { key: "refund", label: "Refund", detail: "" },
    { key: "replace", label: "Send a replacement", detail: "" },
  ],
};

describe("DecisionCard states", () => {
  beforeEach(() => {
    decide.mockReset();
  });

  it("shows the picked option once decided", () => {
    decide.mockImplementation((_vars, opts) => opts.onSuccess());
    render(<DecisionCard approval={decision as never} />);
    fireEvent.click(screen.getByRole("button", { name: "Decide: Send a replacement" }));
    expect(screen.getByText("Decided")).toBeInTheDocument();
    for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
  });

  it("says inline when somebody else already decided", () => {
    decide.mockImplementation((_vars, opts) => opts.onError(new ApiError("conflict", 409)));
    render(<DecisionCard approval={decision as never} />);
    fireEvent.click(screen.getByRole("button", { name: "Decide: Refund (recommended)" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Somebody already decided this.");
    for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
  });

  it("says inline when the decision is not theirs to make", () => {
    decide.mockImplementation((_vars, opts) => opts.onError(new ApiError("forbidden", 403)));
    render(<DecisionCard approval={decision as never} />);
    fireEvent.click(screen.getByRole("button", { name: "Decide: Refund (recommended)" }));
    expect(screen.getByRole("alert")).toHaveTextContent("You may not decide this.");
  });

  it("is read-only for somebody who may not decide", () => {
    render(<DecisionCard approval={decision as never} mayDecide={false} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Send a replacement")).toBeInTheDocument();
    expect(screen.getByText("Recommended")).toBeInTheDocument();
    expect(screen.getByText(/you cannot make it/)).toBeInTheDocument();
  });
});

function message(over: Partial<ChatMessageDTO>): ChatMessageDTO {
  return {
    id: "m",
    sessionId: "s1",
    role: "assistant",
    content: "",
    runId: null,
    renderedComponents: [],
    createdAt: "2026-11-09T10:00:00Z",
    attachments: [],
    mode: null,
    contextRefs: [],
    authorMemberId: null,
    authorName: null,
    replyToId: null,
    ...over,
  };
}

function session(over: Record<string, unknown> = {}) {
  return {
    id: "s1",
    agentId: "a",
    modelOverrideId: null,
    kind: "chat",
    activeRuns: [],
    readOnly: false,
    pendingQuestion: null,
    taskId: "task-1",
    ...over,
  };
}

function chat(suggestions: string[] | undefined, over: Record<string, unknown> = {}) {
  const u1 = message({ id: "u1", role: "user", content: "First", authorMemberId: "me" });
  const a1 = message({
    id: "a1",
    content: "Old answer",
    replyToId: "u1",
    suggestions: ["Old chip"],
  });
  const u2 = message({ id: "u2", role: "user", content: "Second", authorMemberId: "me" });
  const a2 = message({ id: "a2", content: "New answer", replyToId: "u2", suggestions });
  conv.current = {
    session: session(over),
    messages: [u1, a1, u2, a2],
    turns: [
      { kind: "user", key: "u1", message: u1 },
      { kind: "assistant", key: "a1", replyToId: "u1", variants: [a1], liveRunId: null },
      { kind: "user", key: "u2", message: u2 },
      { kind: "assistant", key: "a2", replyToId: "u2", variants: [a2], liveRunId: null },
    ],
    liveRunIds: [],
  };
}

function mount(props: Record<string, unknown> = {}) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <Conversation
        agentId="a"
        sessionId="s1"
        onSessionChange={vi.fn()}
        variant="full"
        {...props}
      />
    </QueryClientProvider>,
  );
}

describe("Conversation suggestions and decisions", () => {
  beforeEach(() => {
    approvals.current = [];
    me.current = { memberId: "me" };
    decide.mockReset();
  });

  it("shows the model's chips on the last answer only and sends one as a message", async () => {
    chat(["Shorter please"]);
    const post = vi.spyOn(api, "post").mockResolvedValue(message({ id: "u3", role: "user" }));
    mount();
    expect(screen.queryByRole("button", { name: "Old chip" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Shorter please" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/chat/sessions/s1/messages",
        expect.objectContaining({ message: "Shorter please" }),
      ),
    );
    post.mockRestore();
  });

  it("puts a chip into the draft with the pencil, keeping what was typed", () => {
    chat(["Send it"]);
    mount();
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Edit before sending: Send it" }));
    expect(box).toHaveValue("Hello Send it");
  });

  it("falls back to the next-step chips when the model gave none", async () => {
    chat([]);
    const post = vi.spyOn(api, "post").mockResolvedValue(message({ id: "u3", role: "user" }));
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Summarise this" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/chat/sessions/s1/messages",
        expect.objectContaining({ message: "/summarise this conversation" }),
      ),
    );
    post.mockRestore();
  });

  it("shows no chips while a run is live, a question waits or the chat is read-only", () => {
    chat(["Shorter please"]);
    conv.current.liveRunIds = ["r9"];
    const { unmount } = mount();
    expect(screen.queryByRole("button", { name: "Shorter please" })).toBeNull();
    unmount();

    chat(["Shorter please"], {
      pendingQuestion: { clarificationId: "c1", runId: "r1", text: "", questions: [] },
    });
    const second = mount();
    expect(screen.queryByRole("button", { name: "Shorter please" })).toBeNull();
    second.unmount();

    chat(["Shorter please"], { readOnly: true });
    mount();
    expect(screen.queryByRole("button", { name: "Shorter please" })).toBeNull();
  });

  it("shows this session's decisions on the last answer, and only those", () => {
    chat(["Shorter please"]);
    me.current = { memberId: "me", seats: [{ departmentId: "d1", seatRole: "dept_approver" }] };
    approvals.current = [
      decision,
      { ...decision, id: "ap2", title: "Another chat's decision", taskId: "task-2" },
      { ...decision, id: "ap3", title: "A plain approval", actionType: "send_mail" },
    ];
    mount();
    expect(screen.getByText("Refund 40 €?")).toBeInTheDocument();
    expect(screen.queryByText("Another chat's decision")).toBeNull();
    expect(screen.queryByText("A plain approval")).toBeNull();
    expect(screen.getByRole("button", { name: "Decide: Send a replacement" })).toBeEnabled();
  });

  it("shows a decision read-only to a member without the approver seat", () => {
    chat(["Shorter please"]);
    me.current = { memberId: "me", seats: [{ departmentId: "d1", seatRole: "dept_viewer" }] };
    approvals.current = [decision];
    mount();
    expect(screen.getByText("Refund 40 €?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Decide:/ })).toBeNull();
  });

  it("shows a decision before the first answer, below the turns", () => {
    const u1 = message({ id: "u1", role: "user", content: "First", authorMemberId: "me" });
    conv.current = {
      session: session(),
      messages: [u1],
      turns: [{ kind: "user", key: "u1", message: u1 }],
      liveRunIds: [],
    };
    approvals.current = [decision];
    mount();
    expect(screen.getAllByText("Refund 40 €?")).toHaveLength(1);
    expect(screen.getByTestId("session-decisions")).toHaveTextContent("Refund 40 €?");
  });

  it("never shows a decision twice when there is an answer", () => {
    chat(["Shorter please"]);
    approvals.current = [decision];
    mount();
    expect(screen.getAllByText("Refund 40 €?")).toHaveLength(1);
    expect(screen.queryByTestId("session-decisions")).toBeNull();
    const answers = screen.getAllByRole("article", { name: "Answer" });
    expect(answers[answers.length - 1]).toHaveTextContent("Refund 40 €?");
  });

  it("shows no decisions for a chat without a task", () => {
    chat(["Shorter please"], { taskId: null });
    approvals.current = [{ ...decision, taskId: null }];
    mount();
    expect(screen.queryByText("Refund 40 €?")).toBeNull();
  });

  it("offers prompt starters on an empty chat and sends the picked one", async () => {
    conv.current = { session: session(), messages: [], turns: [], liveRunIds: [] };
    const post = vi.spyOn(api, "post").mockResolvedValue(message({ id: "u3", role: "user" }));
    mount({
      emptyState: <p>Say hello</p>,
      promptStarters: [{ title: "What is waiting on me?", subtitle: "Approvals and questions" }],
    });
    expect(screen.getByText("Say hello")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /What is waiting on me\?/ }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/chat/sessions/s1/messages",
        expect.objectContaining({ message: "What is waiting on me?" }),
      ),
    );
    post.mockRestore();
  });
});
