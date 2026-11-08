import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({
    session: undefined,
    messages: [],
    turns: [],
    isLoading: true,
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
  useChatModes: () => ({ data: [] }),
}));
vi.mock("@/lib/governance-hooks", () => ({
  useCan: () => () => true,
  // No approval:view: the chat sends no approvals request.
  useMay: () => () => false,
}));

import { Conversation } from "@/components/conversation";

describe("Conversation loading state", () => {
  it("shows turn-shaped skeletons and no spinner", () => {
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation agentId="a" sessionId="s" onSessionChange={vi.fn()} variant="compact" />
      </QueryClientProvider>,
    );
    expect(screen.getAllByTestId("turn-skeleton")).toHaveLength(3);
    expect(container.querySelector(".animate-spin")).toBeNull();
  });
});
