// frontend/src/test/conversation-agent-mode.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const setMode = vi.hoisted(() => vi.fn());
const session = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useSetChatMode: () => ({ mutate: setMode, isPending: false }),
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
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
  useMay: () => () => false,
}));

import { Conversation } from "@/components/conversation";

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

function wrap(sessionId: string | null = "s1") {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <Conversation agentId="a1" sessionId={sessionId} onSessionChange={vi.fn()} variant="full" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  setMode.mockReset();
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
});
