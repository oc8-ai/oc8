// frontend/src/test/conversation-agent-mode.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const setMode = vi.hoisted(() => vi.fn());
const resetMode = vi.hoisted(() => vi.fn());
const modeState = vi.hoisted(() => ({ error: null as unknown }));
const create = vi.hoisted(() => vi.fn());
const session = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useSetChatMode: () => ({
    mutate: setMode,
    reset: resetMode,
    isPending: false,
    error: modeState.error,
  }),
}));
vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({
    session: session.current,
    messages: [],
    turns: [],
    isLoading: false,
    liveRunIds: [],
    addPendingRun: vi.fn(),
  }),
}));
vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useAuth: () => ({ data: { memberId: "me" } }),
  useBudgetStatus: () => ({ data: undefined }),
}));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({
    data: [
      {
        key: "agent",
        summary: "Set up a new agent together.",
        allowsTools: true,
        allowsWrites: false,
        sticky: true,
      },
    ],
  }),
}));
vi.mock("@/lib/hooks-chat", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks-chat")>()),
  useCreateChatSession: () => ({ mutateAsync: create }),
}));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
  useMay: () => () => false,
}));

import { Conversation } from "@/components/conversation";
import { api, ApiError } from "@/lib/api";
import { conversationKeys, type ChatSessionDetailDTO } from "@/lib/conversation-api";

const base = {
  id: "s1",
  agentId: "a1",
  title: "",
  createdAt: "2026-11-09T09:00:00Z",
  lastMessageAt: null,
  modelOverrideId: null,
  kind: "chat",
  activeRuns: [],
  readOnly: false,
  pendingQuestion: null,
  taskId: null,
  pinnedRefs: [],
  projectId: null,
  pinned: false,
};

const client = { current: new QueryClient() };
function tree(sessionId: string | null, agentMode = true) {
  return (
    <QueryClientProvider client={client.current}>
      <Conversation
        agentId="a1"
        sessionId={sessionId}
        onSessionChange={vi.fn()}
        variant="full"
        agentMode={agentMode}
      />
    </QueryClientProvider>
  );
}
function wrap(sessionId: string | null = "s1", agentMode = true) {
  return render(tree(sessionId, agentMode));
}

beforeEach(() => {
  setMode.mockReset();
  resetMode.mockReset();
  modeState.error = null;
  create.mockReset();
  client.current = new QueryClient();
  vi.restoreAllMocks();
});

describe("Agent mode in the chat", () => {
  it("shows the chip while the chat is in agent mode and leaves it on ✕", () => {
    session.current = { ...base, mode: "agent" };
    wrap();
    expect(screen.getByText("Setting up an agent")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Leave agent mode" }));
    expect(setMode).toHaveBeenCalledWith(null);
  });

  it("shows no chip in an ordinary chat", () => {
    session.current = { ...base, mode: null };
    wrap();
    expect(screen.queryByText("Setting up an agent")).not.toBeInTheDocument();
  });

  it("enters the mode on a bare /agent without sending a message", () => {
    session.current = { ...base, mode: null };
    wrap();
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "/agent" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(setMode).toHaveBeenCalledWith("agent");
  });

  it("holds the mode for a chat that does not exist yet", () => {
    session.current = undefined;
    wrap(null);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "/agent" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(setMode).not.toHaveBeenCalled();
    expect(screen.getByText("Setting up an agent")).toBeInTheDocument();
  });

  it("clears a held mode on \u2715 without any request", () => {
    const put = vi.spyOn(api, "put");
    session.current = undefined;
    wrap(null);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "/agent" } });
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Leave agent mode" }));
    expect(screen.queryByText("Setting up an agent")).not.toBeInTheDocument();
    expect(setMode).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
  });

  it("sends the first message of a held mode with /agent and seeds the chat in it", async () => {
    create.mockResolvedValue({ id: "new", agentId: "a1", title: "", createdAt: "" });
    const post = vi.spyOn(api, "post").mockResolvedValue({
      id: "u1",
      sessionId: "new",
      role: "user",
      content: "/agent hello",
      runId: null,
      renderedComponents: [],
      createdAt: "2026-11-09T09:00:00Z",
      attachments: [],
      contextRefs: [],
    });
    session.current = undefined;
    wrap(null);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "/agent" } });
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][0]).toBe("/chat/sessions/new/messages");
    expect(post.mock.calls[0][1]).toMatchObject({ message: "/agent hello" });
    expect(
      client.current.getQueryData<ChatSessionDetailDTO>(conversationKeys.session("new"))?.mode,
    ).toBe("agent");
  });

  it("offers /agent only in the Copilot's chat", () => {
    session.current = { ...base, mode: null };
    wrap("s1", false);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "/" } });
    expect(screen.queryByText("/agent")).not.toBeInTheDocument();
    fireEvent.change(box, { target: { value: "/agent" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(setMode).not.toHaveBeenCalled();
    expect(screen.queryByText("Setting up an agent")).not.toBeInTheDocument();
  });

  it("lists /agent in the Copilot's picker", () => {
    session.current = { ...base, mode: null };
    wrap();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "/" } });
    expect(screen.getByText("/agent")).toBeInTheDocument();
  });

  it("shows a refused switch, keeps the chip, and drops the notice on another chat", () => {
    session.current = { ...base, mode: "agent" };
    modeState.error = new ApiError("Agent mode cannot start from /plan.", 409);
    const view = wrap();
    expect(screen.getByRole("alert")).toHaveTextContent("Agent mode cannot start from /plan.");
    expect(screen.getByText("Setting up an agent")).toBeInTheDocument();
    resetMode.mockClear();
    view.rerender(tree("s2"));
    expect(resetMode).toHaveBeenCalled();
  });
});
