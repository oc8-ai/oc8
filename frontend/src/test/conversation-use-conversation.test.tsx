import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const get = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: { get, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

import { useConversation } from "@/components/conversation/use-conversation";

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

describe("useConversation", () => {
  it("never polls the transcript", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(
        path.endsWith("/messages")
          ? [
              {
                id: "u1",
                sessionId: "s",
                role: "user",
                content: "x",
                runId: null,
                renderedComponents: [],
                createdAt: "",
                attachments: [],
                mode: null,
                contextRefs: [],
                authorMemberId: null,
                authorName: null,
                replyToId: null,
              },
            ]
          : {
              id: "s",
              agentId: "a",
              title: "",
              createdAt: "",
              lastMessageAt: null,
              modelOverrideId: null,
              kind: "chat",
              activeRuns: [{ runId: "r1", replyToId: "u1", state: "running" }],
              readOnly: false,
            },
      ),
    );
    const qc = new QueryClient();
    const { result } = renderHook(() => useConversation("s"), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current.turns).toHaveLength(2));
    expect(result.current.liveRunIds).toEqual(["r1"]);
    const calls = get.mock.calls.length;
    await new Promise((r) => setTimeout(r, 2500));
    expect(get.mock.calls.length).toBe(calls);
  }, 6000);
});
