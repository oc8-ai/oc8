import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const post = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post, put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

import { conversationKeys, useRegenerate, type ChatSessionDetailDTO } from "@/lib/conversation-api";

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const session: ChatSessionDetailDTO = {
  id: "s",
  agentId: "a",
  title: "",
  createdAt: "",
  lastMessageAt: null,
  modelOverrideId: null,
  kind: "chat",
  activeRuns: [{ runId: "r2", replyToId: "u1", state: "running" }],
  readOnly: false,
  mode: null,
};

describe("useRegenerate", () => {
  it("never adds a run id to activeRuns twice", async () => {
    const qc = new QueryClient();
    qc.setQueryData(conversationKeys.session("s"), session);
    post.mockResolvedValue({ runId: "r2", replyToId: "u1", state: "running" });
    const { result } = renderHook(() => useRegenerate("s"), { wrapper: wrapper(qc) });
    await act(() => result.current.mutateAsync("a1"));
    const after = qc.getQueryData<ChatSessionDetailDTO>(conversationKeys.session("s"));
    expect(after?.activeRuns.map((r) => r.runId)).toEqual(["r2"]);
  });

  it("adds a new regeneration run", async () => {
    const qc = new QueryClient();
    qc.setQueryData(conversationKeys.session("s"), session);
    post.mockResolvedValue({ runId: "r3", replyToId: "u1", state: "queued" });
    const { result } = renderHook(() => useRegenerate("s"), { wrapper: wrapper(qc) });
    await act(() => result.current.mutateAsync("a1"));
    const after = qc.getQueryData<ChatSessionDetailDTO>(conversationKeys.session("s"));
    expect(after?.activeRuns.map((r) => r.runId)).toEqual(["r2", "r3"]);
  });
});
