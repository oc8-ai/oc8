// frontend/src/test/conversation-send.test.tsx
// What a send does in the one chat: a model picked before the first message
// (Ruling 51), writing to the own running answer (Ruling 52), send errors by
// status with "Send again" (Ruling 53), the local budget chip, and Stop and
// regenerate failures shown inline.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const conv = vi.hoisted(() => ({
  session: undefined as unknown,
  messages: [] as unknown[],
  turns: [] as unknown[],
  liveRunIds: [] as string[],
}));
const steering = vi.hoisted(() => ({ value: undefined as unknown, asked: [] as unknown[] }));
const create = vi.hoisted(() => vi.fn());
const send = vi.hoisted(() => vi.fn());
const budget = vi.hoisted(() => ({ softExceeded: false }));
const thinks = vi.hoisted(() => ({ value: false }));

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
  useBudgetStatus: () => ({
    data: { currentTokens: 900, hardLimitTokens: 1000, softExceeded: budget.softExceeded },
  }),
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
  useRunSteering: (runId: string | null) => {
    steering.asked.push(runId);
    return { data: runId ? steering.value : undefined };
  },
  // The Live tab's timeline: the member's own chat runs are never on it
  // (they are the session's turns), so it names no running run.
  useLiveTimeline: () => ({
    data: { sessionId: "s", mayViewAllRuns: false, nextBefore: null, items: [] },
    error: null,
    hasOlder: false,
    loadingOlder: false,
    fetchOlder: vi.fn(),
  }),
  useSessionModels: (sessionId: string | null, _enabled: boolean, agentId?: string) => ({
    // A chat that does not exist yet lists its agent's options (Ruling 57).
    data:
      sessionId || agentId
        ? [
            {
              id: null,
              label: "Auto",
              provider: "",
              locality: "",
              isDefault: false,
              supportsReasoning: thinks.value,
              defaultEffort: "balanced",
            },
            {
              id: "m2",
              label: "Llama",
              provider: "ollama",
              locality: "local",
              isDefault: false,
              supportsReasoning: thinks.value,
              defaultEffort: "balanced",
            },
          ]
        : [],
    isLoading: false,
  }),
}));

import { Conversation } from "@/components/conversation";
import { LiveTimeline } from "@/components/conversation/live-timeline";
import { api, ApiError } from "@/lib/api";
import { appendMessage, chatKeys, type ChatMessageDTO } from "@/lib/hooks-chat";

function message(id: string, role: "user" | "assistant", over: Record<string, unknown> = {}) {
  return {
    id,
    sessionId: "s",
    role,
    content: role === "user" ? "hi" : "hello",
    runId: role === "assistant" ? `run-${id}` : null,
    renderedComponents: [],
    createdAt: "2026-11-08T09:00:00Z",
    attachments: [],
    mode: null,
    contextRefs: [],
    authorMemberId: role === "user" ? "me" : null,
    authorName: null,
    replyToId: role === "assistant" ? "u1" : null,
    ...over,
  };
}

function detail(over: Record<string, unknown> = {}) {
  return {
    id: "s",
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
    ...over,
  };
}

function mount(sessionId: string | null, onSessionChange = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <Conversation
        agentId="a"
        sessionId={sessionId}
        onSessionChange={onSessionChange}
        variant="full"
      />
    </QueryClientProvider>,
  );
  return { qc, onSessionChange };
}

function type(text: string) {
  const box = screen.getByRole("textbox", { name: /Message/ });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter" });
  return box;
}

beforeEach(() => {
  conv.session = undefined;
  conv.messages = [];
  conv.turns = [];
  conv.liveRunIds = [];
  steering.value = undefined;
  steering.asked = [];
  budget.softExceeded = false;
  thinks.value = false;
  create.mockReset();
  send.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe("a new chat's model (Rulings 51 and 57)", () => {
  it("is picked without creating a chat, and written before the first message", async () => {
    create.mockResolvedValue({ id: "new", agentId: "a", title: "", createdAt: "" });
    const calls: string[] = [];
    const put = vi.spyOn(api, "put").mockImplementation(async (path: string) => {
      calls.push(`PUT ${path}`);
      return detail({ id: "new", modelOverrideId: "m2" });
    });
    vi.spyOn(api, "post").mockImplementation(async (path: string) => {
      calls.push(`POST ${path}`);
      return message("u1", "user", { sessionId: "new" });
    });
    create.mockImplementation(async () => {
      calls.push("POST /chat/sessions");
      return { id: "new", agentId: "a", title: "", createdAt: "" };
    });
    mount(null);
    // The picker is there in a new chat and lists the agent's models.
    fireEvent.click(screen.getByRole("button", { name: /Model: Auto/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Llama/ }));
    // Nothing was created or written: the pick is held and shown.
    expect(create).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Model: Llama/ })).toBeInTheDocument();

    type("hello");
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(calls).toEqual([
      "POST /chat/sessions",
      "PUT /chat/sessions/new/model",
      "POST /chat/sessions/new/messages",
    ]);
    expect(put).toHaveBeenCalledWith("/chat/sessions/new/model", { modelConfigId: "m2" });
  });

  it("does not send when the held model is refused, and says so", async () => {
    create.mockResolvedValue({ id: "new", agentId: "a", title: "", createdAt: "" });
    vi.spyOn(api, "put").mockRejectedValue(new ApiError("model not allowed", 422));
    const post = vi.spyOn(api, "post");
    mount(null);
    fireEvent.click(screen.getByRole("button", { name: /Model: Auto/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Llama/ }));
    type("hello");
    expect(await screen.findByRole("alert")).toHaveTextContent(/isn't available for this chat/);
    expect(send).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: /Message/ })).toHaveValue("hello");
  });
});

describe("a new chat's reasoning level", () => {
  it("is held and written before the first message", async () => {
    thinks.value = true;
    const calls: string[] = [];
    const put = vi.spyOn(api, "put").mockImplementation(async (path: string) => {
      calls.push(`PUT ${path}`);
      return detail({ id: "new", effort: "thorough" });
    });
    vi.spyOn(api, "post").mockImplementation(async (path: string) => {
      calls.push(`POST ${path}`);
      return message("u1", "user", { sessionId: "new" });
    });
    create.mockImplementation(async () => {
      calls.push("POST /chat/sessions");
      return { id: "new", agentId: "a", title: "", createdAt: "" };
    });
    mount(null);
    fireEvent.click(screen.getByRole("button", { name: /Reasoning:/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Thorough/ }));
    expect(create).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Reasoning: Thorough" })).toBeInTheDocument();

    type("hello");
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(calls).toEqual([
      "POST /chat/sessions",
      "PUT /chat/sessions/new/effort",
      "POST /chat/sessions/new/messages",
    ]);
    expect(put).toHaveBeenCalledWith("/chat/sessions/new/effort", { effort: "thorough" });
  });

  it("keeps the held pick and says so when writing it fails, then sends it again", async () => {
    thinks.value = true;
    create.mockResolvedValue({ id: "new", agentId: "a", title: "", createdAt: "" });
    const put = vi
      .spyOn(api, "put")
      .mockRejectedValueOnce(new ApiError("unavailable", 503))
      .mockResolvedValue(detail({ id: "new", effort: "thorough" }));
    const post = vi
      .spyOn(api, "post")
      .mockResolvedValue(message("u1", "user", { sessionId: "new" }));
    mount(null);
    fireEvent.click(screen.getByRole("button", { name: /Reasoning:/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Thorough/ }));

    type("hello");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /reasoning level you picked couldn't be set/,
    );
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: /Message/ })).toHaveValue("hello");
    // The pick is still held and shown.
    expect(screen.getByRole("button", { name: "Reasoning: Thorough" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Send again/ }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(put).toHaveBeenCalledTimes(2);
    expect(put).toHaveBeenLastCalledWith("/chat/sessions/new/effort", { effort: "thorough" });
  });

  it("is not offered while the chat's model cannot think", () => {
    mount(null);
    expect(screen.queryByRole("button", { name: /Reasoning:/ })).toBeNull();
  });
});

describe("writing to the own running answer (Ruling 52)", () => {
  beforeEach(() => {
    conv.session = detail({ activeRuns: [{ runId: "r1", replyToId: "u1", state: "running" }] });
    conv.messages = [message("u1", "user")];
    conv.turns = [
      { kind: "user", key: "u1", message: message("u1", "user") },
      { kind: "assistant", key: "answer-u1", replyToId: "u1", variants: [], liveRunId: "r1" },
    ];
    conv.liveRunIds = ["r1"];
  });

  it("sends to POST /runs/{id}/inputs with the hint, and Stop stays", async () => {
    steering.value = { mayWrite: true, acceptsInputs: true, inputs: [] };
    const post = vi.spyOn(api, "post").mockResolvedValue({
      id: "i1",
      text: "only March",
      authorName: "Me",
      createdAt: "",
      consumedAt: null,
      consumedStep: null,
    });
    mount("s");
    expect(screen.getByText("Goes to the running answer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop the answer" })).toBeInTheDocument();
    type("only March");
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/runs/r1/inputs", { text: "only March" }),
    );
    expect(post).not.toHaveBeenCalledWith("/runs/r1/stop");
    expect(send).not.toHaveBeenCalled();
    // Only the run being steered is asked for its steering state.
    expect(new Set(steering.asked.filter(Boolean))).toEqual(new Set(["r1"]));
  });

  it("in the Live tab, sends to the member's own running run (E2E A)", async () => {
    conv.session = detail({
      kind: "live",
      activeRuns: [{ runId: "r1", replyToId: "u1", state: "running" }],
    });
    steering.value = { mayWrite: true, acceptsInputs: true, inputs: [] };
    const post = vi.spyOn(api, "post").mockResolvedValue({
      id: "i1",
      text: "only March",
      authorName: "Me",
      createdAt: "",
      consumedAt: null,
      consumedStep: null,
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <LiveTimeline agentId="a" departmentId="d" promptStarters={[]} />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Goes to the running answer")).toBeInTheDocument();
    const box = type("only March");
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/runs/r1/inputs", { text: "only March" }),
    );
    expect(send).not.toHaveBeenCalled();
    expect(box).toHaveValue("");
  });

  it("keeps Send away while the run takes no messages", () => {
    steering.value = { mayWrite: true, acceptsInputs: false, inputs: [] };
    mount("s");
    expect(screen.queryByText("Goes to the running answer")).toBeNull();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(screen.getByRole("button", { name: "Stop the answer" })).toBeInTheDocument();
  });

  it("shows a failed Stop inline", async () => {
    steering.value = { mayWrite: true, acceptsInputs: false, inputs: [] };
    vi.spyOn(api, "post").mockRejectedValue(new ApiError("boom", 500));
    mount("s");
    fireEvent.click(screen.getByRole("button", { name: "Stop the answer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The answer was not stopped.");
  });

  it("treats a 409 on Stop as already finished", async () => {
    steering.value = { mayWrite: true, acceptsInputs: false, inputs: [] };
    const post = vi.spyOn(api, "post").mockRejectedValue(new ApiError("finished", 409));
    mount("s");
    fireEvent.click(screen.getByRole("button", { name: "Stop the answer" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/runs/r1/stop"));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("send errors by status (Ruling 53)", () => {
  beforeEach(() => {
    conv.session = detail();
  });

  it.each([
    [new TypeError("Failed to fetch"), /isn't reachable right now/, true],
    [new ApiError("down", 503), /isn't reachable right now/, true],
    [new ApiError("no", 403), /You may not write to this agent/, false],
    [new ApiError("Message is empty after the command.", 422), /Message is empty after/, false],
  ])("words %s", async (error, text, retry) => {
    send.mockRejectedValue(error);
    mount("s");
    type("hello");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(text);
    expect(!!screen.queryByRole("button", { name: "Send again" })).toBe(retry);
    expect(screen.getByRole("textbox", { name: /Message/ })).toHaveValue("hello");
  });

  it("resends the restored draft with Send again", async () => {
    send.mockRejectedValueOnce(new ApiError("down", 502));
    send.mockResolvedValueOnce(message("u1", "user"));
    mount("s");
    type("hello");
    fireEvent.click(await screen.findByRole("button", { name: "Send again" }));
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(send).toHaveBeenLastCalledWith({ message: "hello", attachmentIds: [], contextRefs: [] });
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("starts a new chat when this one no longer exists", async () => {
    send.mockRejectedValue(new ApiError("not found", 404));
    const { onSessionChange } = mount("s");
    type("hello");
    expect(await screen.findByRole("alert")).toHaveTextContent(/This chat no longer exists/);
    expect(onSessionChange).toHaveBeenCalledWith(null);
  });
});

describe("chips and answer actions", () => {
  beforeEach(() => {
    conv.session = detail();
    conv.messages = [message("u1", "user"), message("a1", "assistant")];
    conv.turns = [
      { kind: "user", key: "u1", message: message("u1", "user") },
      {
        kind: "assistant",
        key: "answer-u1",
        replyToId: "u1",
        variants: [message("a1", "assistant")],
        liveRunId: null,
      },
    ];
  });

  it("opens the budget card from 'Budget is running low' and sends nothing", async () => {
    budget.softExceeded = true;
    const post = vi.spyOn(api, "post");
    mount("s");
    fireEvent.click(screen.getByRole("button", { name: "Budget is running low" }));
    expect(await screen.findByText(/900 tokens used this month of 1,000/)).toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it("offers no chips under a failed answer (E2E F)", () => {
    budget.softExceeded = true;
    const render_ = (state: string) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      // The answer's run, as the turn has already loaded it.
      qc.setQueryData(["run", "run-a1"], { id: "run-a1", state, toolCalls: [], steps: 0 });
      return render(
        <QueryClientProvider client={qc}>
          <Conversation agentId="a" sessionId="s" onSessionChange={vi.fn()} variant="full" />
        </QueryClientProvider>,
      );
    };
    const done = render_("done");
    expect(screen.getByRole("button", { name: "Budget is running low" })).toBeInTheDocument();
    done.unmount();
    render_("failed");
    expect(screen.queryByRole("button", { name: "Budget is running low" })).toBeNull();
  });

  it("shows a refused regenerate inline", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new ApiError("busy", 409));
    vi.spyOn(api, "get").mockResolvedValue(undefined);
    mount("s");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Regenerate answer" }));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(/still being written/);
  });
});

describe("a sent message shows once (I2)", () => {
  it("appendMessage never adds a message the cache already holds", () => {
    const m = message("u1", "user") as unknown as ChatMessageDTO;
    expect(appendMessage(undefined, m)).toEqual([m]);
    const prev = [m];
    expect(appendMessage(prev, m)).toBe(prev);
  });

  it("keeps one copy when the live refetch lands before the POST reply", async () => {
    create.mockResolvedValue({ id: "new", agentId: "a", title: "", createdAt: "" });
    const sent = message("u1", "user", { sessionId: "new" });
    let qc: QueryClient | null = null;
    vi.spyOn(api, "post").mockImplementation(async () => {
      // chat.message_created's refetch resolved first.
      qc?.setQueryData(chatKeys.messages("new"), [sent]);
      return sent;
    });
    ({ qc } = mount(null));
    type("hi");
    await waitFor(() => expect(qc?.getQueryData(chatKeys.messages("new"))).toEqual([sent]));
  });
});
