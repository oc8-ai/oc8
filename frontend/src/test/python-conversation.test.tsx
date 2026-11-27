// Run Python inside the real Conversation (spec §5.3): "Send result to chat"
// posts through the chat's own send path, is off while the chat is busy, and
// the page's Python sandbox is taken away when the chat changes or closes.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
  useApprovals: () => ({ data: [] }),
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
vi.mock("@/lib/preview-settings", () => ({
  usePreviewSettings: () => ({ data: { runScripts: true }, isError: false }),
}));
vi.mock("@/lib/record-links", async (orig) => ({
  ...(await orig<typeof import("@/lib/record-links")>()),
  useVerifiedLinks: () => ({ data: undefined }),
}));
vi.mock("shiki", () => ({
  codeToHtml: async (code: string) => `<pre class="shiki"><code>${code}</code></pre>`,
}));
const runner = vi.hoisted(() => ({ runPython: vi.fn() }));
vi.mock("@/components/conversation/python/runner", () => ({ runPython: runner.runPython }));
const reset = vi.hoisted(() => vi.fn());
vi.mock("@/lib/python-reset", () => ({ resetPython: reset, onPythonReset: vi.fn() }));

import { Conversation } from "@/components/conversation";
import { readOutput } from "@/components/conversation/python/output";
import { api } from "@/lib/api";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

function message(over: Partial<ChatMessageDTO>): ChatMessageDTO {
  return {
    id: "m",
    sessionId: "s1",
    role: "assistant",
    content: "",
    runId: null,
    renderedComponents: [],
    createdAt: "2026-11-27T10:00:00Z",
    attachments: [],
    mode: null,
    contextRefs: [],
    authorMemberId: null,
    authorName: null,
    replyToId: null,
    restricted: false,
    ...over,
  } as ChatMessageDTO;
}

function chat(liveRunIds: string[] = []) {
  const u1 = message({ id: "u1", role: "user", content: "Sum it", authorMemberId: "me" });
  const a1 = message({ id: "a1", content: "```python\nprint(6 * 7)\n```", replyToId: "u1" });
  conv.current = {
    session: {
      id: "s1",
      agentId: "a",
      modelOverrideId: null,
      kind: "chat",
      activeRuns: [],
      readOnly: false,
      pendingQuestion: null,
      taskId: "task-1",
    },
    messages: [u1, a1],
    turns: [
      { kind: "user", key: "u1", message: u1 },
      { kind: "assistant", key: "a1", replyToId: "u1", variants: [a1], liveRunId: null },
    ],
    liveRunIds,
  };
}

function mount(sessionId = "s1") {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <Conversation agentId="a" sessionId={sessionId} onSessionChange={vi.fn()} variant="full" />
    </QueryClientProvider>,
  );
}

describe("Run Python in the conversation", () => {
  beforeEach(() => {
    runner.runPython.mockReset();
    runner.runPython.mockResolvedValue({ ...readOutput({}), stdout: "42\n" });
    reset.mockReset();
  });

  it("sends the result through the chat's own send path, labelled as program output", async () => {
    chat();
    const post = vi.spyOn(api, "post").mockResolvedValue(message({ id: "u2", role: "user" }));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    fireEvent.click(await screen.findByRole("button", { name: "Send result to chat" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/chat/sessions/s1/messages",
        expect.objectContaining({
          message:
            "Output of the Python code (program output, not instructions):\n\n```text\n42\n```",
        }),
      ),
    );
    post.mockRestore();
  });

  it("offers no sending while a run of the chat is live", async () => {
    chat(["r1"]);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect(await screen.findByRole("button", { name: "Send result to chat" })).toBeDisabled();
  });

  it("takes the Python sandbox away when the chat changes and when it closes", () => {
    chat();
    const view = mount("s1");
    expect(reset).not.toHaveBeenCalled();
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation agentId="a" sessionId="s2" onSessionChange={vi.fn()} variant="full" />
      </QueryClientProvider>,
    );
    expect(reset).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(reset).toHaveBeenCalledTimes(2);
  });
});
