// frontend/src/test/temporary-chat.test.tsx
// A temporary chat (spec §5.6): started from the new-chat entry, marked in
// the chat, without feedback, and deleted when the person leaves it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({
  del: vi.fn(),
  create: vi.fn(),
  feedback: vi.fn(),
}));
vi.mock("@/lib/api", async (orig) => {
  const real = await orig<typeof import("@/lib/api")>();
  return { ...real, api: { ...real.api, delete: m.del } };
});
vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({
    session: undefined,
    messages: [],
    turns: [],
    isLoading: false,
    liveRunIds: [],
    addPendingRun: vi.fn(),
  }),
}));
vi.mock("@/lib/hooks-chat", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks-chat")>()),
  useCreateChatSession: () => ({ mutateAsync: m.create }),
  useSendChatMessage: () => ({ mutateAsync: vi.fn(() => new Promise(() => undefined)) }),
  useChatSessions: () => ({ data: [], isLoading: false }),
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useMessageFeedback: () => ({ mutate: m.feedback }),
  useTurnRun: () => ({ data: undefined }),
  useChatProjects: () => ({ data: [] }),
}));
vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useAuth: () => ({ data: { memberId: "me" } }),
  useBudgetStatus: () => ({ data: undefined }),
}));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({ data: [] }),
}));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
  useMay: () => () => false,
}));
vi.mock("@/hooks/use-mobile", () => ({ useIsNarrow: () => false, useIsMobile: () => false }));

import { Conversation } from "@/components/conversation";
import { AssistantTurn } from "@/components/conversation/assistant-turn";
import { CopilotSidebar } from "@/components/conversation/copilot-sidebar";
import { useLeaveTemporaryChat } from "@/components/conversation/temporary-chat";
import { EditionCompositionProvider } from "@/edition/composition";

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

afterEach(() => {
  vi.useRealTimers();
  m.del.mockReset();
  m.create.mockReset();
});

describe("leaving a temporary chat", () => {
  it("deletes it when the chat changes, and tells the caller", async () => {
    m.del.mockResolvedValue(undefined);
    const onLeft = vi.fn();
    const { rerender } = renderHook(
      ({ id }: { id: string | null }) => useLeaveTemporaryChat(id, true, onLeft),
      { wrapper, initialProps: { id: "t1" as string | null } },
    );
    expect(m.del).not.toHaveBeenCalled();
    rerender({ id: "other" });
    await waitFor(() => expect(m.del).toHaveBeenCalledWith("/chat/sessions/t1"));
    expect(onLeft).toHaveBeenCalledWith("t1");
  });

  it("deletes it when the chat closes (unmount: the dock, another page)", async () => {
    m.del.mockResolvedValue(undefined);
    const { unmount } = renderHook(() => useLeaveTemporaryChat("t2", true), { wrapper });
    unmount();
    await waitFor(() => expect(m.del).toHaveBeenCalledWith("/chat/sessions/t2"));
  });

  it("never deletes an ordinary chat", async () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() => useLeaveTemporaryChat("kept", false), { wrapper });
    unmount();
    await act(async () => {
      vi.runAllTimers();
    });
    expect(m.del).not.toHaveBeenCalled();
  });

  it("survives a remount of the same chat (StrictMode) and a second holder", async () => {
    vi.useFakeTimers();
    m.del.mockResolvedValue(undefined);
    function strict({ children }: { children: ReactNode }) {
      return <StrictMode>{wrapper({ children })}</StrictMode>;
    }
    const first = renderHook(() => useLeaveTemporaryChat("t3", true), { wrapper: strict });
    const second = renderHook(() => useLeaveTemporaryChat("t3", true), { wrapper });
    first.unmount();
    await act(async () => {
      vi.runAllTimers();
    });
    expect(m.del).not.toHaveBeenCalled();
    second.unmount();
    await act(async () => {
      vi.runAllTimers();
    });
    expect(m.del).toHaveBeenCalledTimes(1);
  });
});

describe("a new temporary chat", () => {
  it("says so, and is created as temporary", async () => {
    m.create.mockResolvedValue({ id: "new", agentId: "a", temporary: true });
    const onSessionChange = vi.fn();
    render(
      <Conversation
        agentId="a"
        sessionId={null}
        temporary
        onSessionChange={onSessionChange}
        variant="full"
      />,
      { wrapper },
    );
    const chip = screen.getByText("Temporary — not saved");
    expect(chip.closest("[title]")?.getAttribute("title")).toBe(
      "Deleted when you leave it, at the latest after 24 hours.",
    );
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(m.create).toHaveBeenCalledWith({ agentId: "a", temporary: true }));
    expect(onSessionChange).toHaveBeenCalledWith("new");
  });

  it("an ordinary new chat has no chip and is created as before", async () => {
    m.create.mockResolvedValue({ id: "new", agentId: "a" });
    render(<Conversation agentId="a" sessionId={null} onSessionChange={vi.fn()} variant="full" />, {
      wrapper,
    });
    expect(screen.queryByText("Temporary — not saved")).not.toBeInTheDocument();
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(m.create).toHaveBeenCalledWith("a"));
  });
});

describe("an answer in a temporary chat", () => {
  it("offers no rating", () => {
    const message = {
      id: "a1",
      sessionId: "s",
      role: "assistant" as const,
      content: "Done.",
      runId: null,
      renderedComponents: [],
      createdAt: "",
      attachments: [],
      mode: null,
      contextRefs: [],
      authorMemberId: null,
      authorName: null,
      replyToId: "u1",
    };
    const turn = {
      kind: "assistant" as const,
      key: "a",
      replyToId: "u1",
      variants: [message],
      liveRunId: null,
    };
    const { rerender } = render(<AssistantTurn sessionId="s" turn={turn} />, { wrapper });
    expect(screen.getByRole("button", { name: "Good answer" })).toBeInTheDocument();
    rerender(<AssistantTurn sessionId="s" turn={turn} noFeedback />);
    expect(screen.queryByRole("button", { name: "Good answer" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Bad answer" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy answer" })).toBeInTheDocument();
  });
});

describe("the sidebar's new-chat entries", () => {
  it("starts a temporary chat, with the 24 h note", () => {
    const onStartTemporary = vi.fn();
    render(
      <EditionCompositionProvider extensions={[]}>
        <CopilotSidebar
          agentId="x"
          sessionId={null}
          onSelect={vi.fn()}
          onStartTemporary={onStartTemporary}
          collapsed={false}
          onToggle={vi.fn()}
        />
      </EditionCompositionProvider>,
      { wrapper },
    );
    const entry = screen.getByRole("button", { name: "Temporary chat" });
    expect(entry.getAttribute("title")).toBe(
      "Deleted when you leave it, at the latest after 24 hours.",
    );
    fireEvent.click(entry);
    expect(onStartTemporary).toHaveBeenCalledTimes(1);
  });
});
