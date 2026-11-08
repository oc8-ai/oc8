import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const send = vi.hoisted(() => vi.fn());
const create = vi.hoisted(() => vi.fn());
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
  useCreateChatSession: () => ({ mutateAsync: create }),
  useSendChatMessage: () => ({ mutateAsync: send }),
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
vi.mock("@/lib/governance-hooks", () => ({ useCan: () => () => true }));

import { Conversation } from "@/components/conversation";
import { api } from "@/lib/api";

describe("Conversation", () => {
  it("creates the session lazily on the first send", async () => {
    create.mockResolvedValue({ id: "new", agentId: "a" });
    send.mockResolvedValue({ id: "u1", runId: "r1" });
    const onSessionChange = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation
          agentId="a"
          sessionId={null}
          onSessionChange={onSessionChange}
          variant="full"
          emptyState={<p>Say hello</p>}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Say hello")).toBeInTheDocument();
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "hi" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(onSessionChange).toHaveBeenCalledWith("new"));
    expect(create).toHaveBeenCalledWith("a");
  });

  it("sends a prompt starter from the empty state as the first message", async () => {
    create.mockResolvedValue({ id: "fresh", agentId: "a" });
    const post = vi.spyOn(api, "post").mockResolvedValue({ id: "u2", runId: null });
    const onSessionChange = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation
          agentId="a"
          sessionId={null}
          onSessionChange={onSessionChange}
          variant="compact"
          emptyState={(sendText) => (
            <button onClick={() => sendText("What is waiting on me?")}>Starter</button>
          )}
        />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByText("Starter"));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/chat/sessions/fresh/messages", {
        message: "What is waiting on me?",
        attachmentIds: [],
        contextRefs: [],
      }),
    );
    expect(onSessionChange).toHaveBeenCalledWith("fresh");
  });

  it('answers "/budget" on the spot without sending a message', async () => {
    send.mockClear();
    create.mockClear();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation agentId="a" sessionId="s" onSessionChange={vi.fn()} variant="compact" />
      </QueryClientProvider>,
    );
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "/budget" } });
    fireEvent.keyDown(box, { key: "Escape" });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText("Budget")).toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
  });
});
