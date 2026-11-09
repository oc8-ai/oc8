// frontend/src/test/conversation-approvals-gate.test.tsx
// The chat reads pending approvals only for a caller holding approval:view;
// anybody else sends no request at all (not one that 403s).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const granted = vi.hoisted(() => ({ current: [] as string[] }));
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
  useMay: () => (permission: string) => granted.current.includes(permission),
}));

import { Conversation } from "@/components/conversation";
import { api } from "@/lib/api";

function mount() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Conversation agentId="a" sessionId={null} onSessionChange={vi.fn()} variant="full" />
    </QueryClientProvider>,
  );
}

const approvalCalls = (get: ReturnType<typeof vi.spyOn>) =>
  get.mock.calls.filter(([url]) => String(url).startsWith("/approvals"));

describe("Conversation approvals gate", () => {
  afterEach(() => vi.restoreAllMocks());

  it("fetches no approvals without approval:view", async () => {
    granted.current = [];
    const get = vi.spyOn(api, "get").mockResolvedValue([]);
    mount();
    // Let any enabled query fire before asserting that none did.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(approvalCalls(get)).toHaveLength(0);
  });

  it("fetches pending approvals with approval:view", async () => {
    granted.current = ["approval:view"];
    const get = vi.spyOn(api, "get").mockResolvedValue([]);
    mount();
    await waitFor(() => expect(approvalCalls(get)).toHaveLength(1));
    expect(approvalCalls(get)[0][0]).toBe("/approvals?status=pending");
  });
});
