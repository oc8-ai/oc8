// From the moment a message is sent until its answer's first word, the turn
// shows the breathing mark -- also before the send has returned and the run
// is known, in the dock (compact) and on the chat page (full) alike. The
// server's rows take over under the same React keys, so the mark is not
// remounted when the run appears, and it stays at least PRESENCE_MIN_MS.
import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const conv = vi.hoisted(() => ({
  session: undefined as unknown,
  messages: [] as unknown[],
  turns: [] as unknown[],
  liveRunIds: [] as string[],
}));
const runs = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
const create = vi.hoisted(() => vi.fn());
const send = vi.hoisted(() => vi.fn());

vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({
    session: conv.session,
    messages: conv.messages,
    turns: conv.turns,
    isLoading: false,
    liveRunIds: conv.liveRunIds,
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
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
  useMay: () => () => false,
  useMayManageAgent: () => () => false,
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: (_sessionId: string | null, runId: string | null) => ({
    data: runId ? runs.value[runId] : undefined,
  }),
  useRunSteering: () => ({ data: undefined }),
  useSessionModels: () => ({ data: [], isLoading: false }),
}));

import { Conversation } from "@/components/conversation";
import { PRESENCE_MIN_MS } from "@/components/conversation/assistant-turn";
import { api } from "@/lib/api";

function message(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    sessionId: "s",
    role: "user",
    content: "hello",
    runId: "r1",
    renderedComponents: [],
    createdAt: "2026-11-10T09:00:00Z",
    attachments: [],
    mode: null,
    contextRefs: [],
    authorMemberId: "me",
    authorName: null,
    replyToId: null,
    ...over,
  };
}

function detail(id = "s") {
  return {
    id,
    agentId: "a",
    title: "",
    createdAt: "",
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
    mode: null,
  };
}

const RUN = {
  id: "r1",
  agentId: "a",
  state: "queued",
  phase: null,
  output: null,
  steps: 0,
  toolCalls: [],
  taskId: null,
  question: null,
  renderedComponents: [],
};

/** The send has returned: the transcript has the message and its live run. */
function landed(sessionId = "s") {
  const m = message("u1", { sessionId });
  conv.session = { ...detail(sessionId), activeRuns: [{ runId: "r1", replyToId: "u1" }] };
  conv.messages = [m];
  conv.turns = [
    { kind: "user", key: "u1", message: m },
    { kind: "assistant", key: "answer-u1", replyToId: "u1", variants: [], liveRunId: "r1" },
  ];
  conv.liveRunIds = ["r1"];
}

function Surface({ variant, initial }: { variant: "full" | "compact"; initial: string | null }) {
  const [sessionId, setSessionId] = useState<string | null>(initial);
  return (
    <Conversation
      agentId="a"
      sessionId={sessionId}
      onSessionChange={setSessionId}
      variant={variant}
      agentMode
    />
  );
}

function mount(variant: "full" | "compact", initial: string | null = "s") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // A new element each time, so React renders again with the mocks' values.
  const ui = () => (
    <QueryClientProvider client={qc}>
      <Surface variant={variant} initial={initial} />
    </QueryClientProvider>
  );
  const result = render(ui());
  return { ...result, rerender: () => result.rerender(ui()) };
}

function type(text: string) {
  const box = screen.getByRole("textbox", { name: /Message/ });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter" });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => {
  conv.session = detail();
  conv.messages = [];
  conv.turns = [];
  conv.liveRunIds = [];
  runs.value = {};
  create.mockReset();
  send.mockReset();
});

describe.each([
  ["the dock", "compact" as const],
  ["the chat page", "full" as const],
])("presence from send to first word, in %s", (_name, variant) => {
  it("shows the message and the mark while the send is on its way (no run yet)", async () => {
    const reply = deferred<ReturnType<typeof message>>();
    send.mockReturnValue(reply.promise);
    mount(variant);
    type("hello");

    // Sent, nothing back yet: the user's turn and the waiting answer.
    expect(await screen.findByTestId("thinking-presence")).toHaveTextContent("Thinking …");
    expect(screen.getByRole("article", { name: "Your message" })).toHaveTextContent("hello");
    expect(screen.getByTestId("presence-mark")).toHaveAttribute("aria-hidden", "true");
    await act(async () => reply.resolve(message("u1")));
  });

  it("keeps the same mark when the run becomes known, then gives way to the words", async () => {
    const reply = deferred<ReturnType<typeof message>>();
    send.mockReturnValue(reply.promise);
    const view = mount(variant);
    type("hello");
    const mark = await screen.findByTestId("presence-mark");
    const userTurn = screen.getByRole("article", { name: "Your message" });

    // The send returns; the transcript has the message and its queued run.
    landed();
    await act(async () => reply.resolve(message("u1")));
    view.rerender();
    // Not remounted: the very same nodes, so the breathing does not restart
    // and the turn does not fade in a second time.
    expect(screen.getByTestId("presence-mark")).toBe(mark);
    expect(screen.getByRole("article", { name: "Your message" })).toBe(userTurn);
    expect(screen.getAllByTestId("thinking-presence")).toHaveLength(1);

    // The first words: the mark stays its minimum, then the answer shows.
    runs.value = { r1: { ...RUN, state: "running", liveAnswer: "Hi there. " } };
    view.rerender();
    expect(screen.getByTestId("thinking-presence")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("thinking-presence")).toBeNull(), {
      timeout: PRESENCE_MIN_MS + 1000,
    });
    await waitFor(() => expect(screen.getByTestId("stream-tail")).toHaveTextContent("Hi there."));
  });

  it("drops the waiting turn when the send fails", async () => {
    send.mockRejectedValue(new Error("network"));
    mount(variant);
    type("hello");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByTestId("thinking-presence")).toBeNull();
    expect(screen.queryByRole("article", { name: "Your message" })).toBeNull();
  });
});

describe("a first message in a new chat", () => {
  it("shows the mark before the chat exists and keeps it once it does", async () => {
    const made = deferred<{ id: string; agentId: string; title: string; createdAt: string }>();
    create.mockReturnValue(made.promise);
    const reply = deferred<ReturnType<typeof message>>();
    // The first message of a new chat goes out by its new id, not the hook.
    const post = vi.spyOn(api, "post").mockReturnValue(reply.promise);
    conv.session = undefined;
    mount("compact", null);
    type("hello");

    expect(await screen.findByTestId("thinking-presence")).toBeInTheDocument();
    const mark = screen.getByTestId("presence-mark");

    conv.session = detail("new");
    await act(async () => made.resolve({ id: "new", agentId: "a", title: "", createdAt: "" }));
    // The chat exists (sessionId "new"), the message is still on its way.
    expect(screen.getByTestId("presence-mark")).toBe(mark);
    expect(screen.getByRole("article", { name: "Your message" })).toHaveTextContent("hello");

    expect(post).toHaveBeenCalledWith("/chat/sessions/new/messages", expect.anything());
    landed("new");
    await act(async () => reply.resolve(message("u1", { sessionId: "new" })));
    expect(screen.getByTestId("presence-mark")).toBe(mark);
    post.mockRestore();
  });
});

describe("a message the server stored without a run", () => {
  it("takes the waiting answer away once the message lands", async () => {
    const reply = deferred<ReturnType<typeof message>>();
    send.mockReturnValue(reply.promise);
    const view = mount("full");
    type("hello");
    expect(await screen.findByTestId("thinking-presence")).toBeInTheDocument();

    const m = message("u1", { runId: null });
    conv.messages = [m];
    conv.turns = [{ kind: "user", key: "u1", message: m }];
    await act(async () => reply.resolve(m));
    view.rerender();
    await waitFor(() => expect(screen.queryByTestId("thinking-presence")).toBeNull(), {
      timeout: PRESENCE_MIN_MS + 1000,
    });
    expect(screen.getAllByRole("article", { name: "Your message" })).toHaveLength(1);
  });
});
