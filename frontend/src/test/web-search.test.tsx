// frontend/src/test/web-search.test.tsx
// Web search (spec §10): the composer's "Web" switch, shown only while the
// tenant has a provider, remembered in this browser and sent with a message,
// a regenerate and an edit; web sources as external links; and the settings
// panel, whose API key is write-only.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const conv = vi.hoisted(() => ({
  session: undefined as unknown,
  messages: [] as unknown[],
  turns: [] as unknown[],
}));
const send = vi.hoisted(() => vi.fn());
const may = vi.hoisted(() => ({ manage: true }));

vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({
    session: conv.session,
    messages: conv.messages,
    turns: conv.turns,
    isLoading: false,
    liveRunIds: [],
    addPendingRun: vi.fn(),
  }),
}));
vi.mock("@/lib/hooks-chat", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks-chat")>()),
  useCreateChatSession: () => ({ mutateAsync: vi.fn() }),
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
  useMay: () => () => may.manage,
  useMayManageAgent: () => () => false,
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useRunSteering: () => ({ data: undefined }),
  useLiveTimeline: () => ({
    data: { sessionId: "s", mayViewAllRuns: false, nextBefore: null, items: [] },
    error: null,
    hasOlder: false,
    loadingOlder: false,
    fetchOlder: vi.fn(),
  }),
  useSessionModels: () => ({ data: [], isLoading: false }),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { Conversation } from "@/components/conversation";
import { Sources } from "@/components/conversation/sources";
import { WebSearchSetting } from "@/components/settings/web-search-setting";
import { api, ApiError } from "@/lib/api";
import type { ChatSourceDTO } from "@/lib/hooks-chat";
import { WEB_CHOICE_STORAGE_KEY } from "@/lib/web-search";

const availability = { available: true };
let settings: Record<string, unknown> = {};

function message(id: string, role: "user" | "assistant") {
  return {
    id,
    sessionId: "s",
    role,
    content: role === "user" ? "hi" : "hello",
    runId: role === "assistant" ? `run-${id}` : null,
    renderedComponents: [],
    createdAt: "2026-11-27T09:00:00Z",
    attachments: [],
    mode: null,
    contextRefs: [],
    authorMemberId: role === "user" ? "me" : null,
    authorName: null,
    replyToId: role === "assistant" ? "u1" : null,
  };
}

function detail() {
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
    currentLeafId: "a1",
  };
}

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

function mountChat() {
  return wrap(<Conversation agentId="a" sessionId="s" onSessionChange={vi.fn()} variant="full" />);
}

function type(text: string) {
  const box = screen.getByRole("textbox", { name: /Message/ });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter" });
}

beforeEach(() => {
  window.localStorage.clear();
  availability.available = true;
  settings = { provider: null, baseUrl: null, hasKey: false, source: null };
  may.manage = true;
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
  send.mockReset().mockResolvedValue(message("u2", "user"));
  vi.spyOn(api, "get").mockImplementation(async (path: string) => {
    if (path === "/chat/web-search") return availability;
    if (path === "/settings/web-search") return settings;
    return undefined;
  });
});
afterEach(() => vi.restoreAllMocks());

describe("the composer's Web switch", () => {
  it("is hidden while the tenant has no search provider", async () => {
    availability.available = false;
    mountChat();
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/chat/web-search"));
    expect(screen.queryByRole("button", { name: "Web" })).toBeNull();
    type("hello");
    await waitFor(() => expect(send).toHaveBeenCalled());
    expect(send.mock.calls[0][0]).not.toHaveProperty("web");
  });

  it("is off by default and sends no web flag", async () => {
    mountChat();
    const toggle = await screen.findByRole("button", { name: "Web" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(toggle).toHaveAttribute("title", "Search the web for this message");
    type("hello");
    await waitFor(() => expect(send).toHaveBeenCalled());
    expect(send.mock.calls[0][0]).not.toHaveProperty("web");
  });

  it("sends web: true once switched on, and remembers the choice", async () => {
    const first = mountChat();
    fireEvent.click(await screen.findByRole("button", { name: "Web" }));
    expect(screen.getByRole("button", { name: "Web" })).toHaveAttribute("aria-pressed", "true");
    expect(window.localStorage.getItem(WEB_CHOICE_STORAGE_KEY)).toBe("1");
    type("what's new today?");
    await waitFor(() => expect(send).toHaveBeenCalled());
    expect(send.mock.calls[0][0]).toEqual({
      message: "what's new today?",
      attachmentIds: [],
      contextRefs: [],
      web: true,
    });
    first.unmount();
    // A new visit in the same browser starts with it on.
    mountChat();
    expect(await screen.findByRole("button", { name: "Web" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("goes with a regenerate and an edit", async () => {
    window.localStorage.setItem(WEB_CHOICE_STORAGE_KEY, "1");
    const post = vi
      .spyOn(api, "post")
      .mockImplementation(async (path: string) =>
        path.endsWith("/regenerate")
          ? { runId: "r2", replyToId: "u1", state: "queued" }
          : { ...message("u3", "user"), content: "fixed" },
      );
    mountChat();
    await screen.findByRole("button", { name: "Web" });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Regenerate answer" }));
    });
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/chat/sessions/s/messages/u1/regenerate", { web: true }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Edit message" }), {
      target: { value: "fixed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/chat/sessions/s/messages/u1/edit", {
        content: "fixed",
        web: true,
      }),
    );
  });

  it("sends nothing about the web when switched off for a regenerate", async () => {
    const post = vi
      .spyOn(api, "post")
      .mockResolvedValue({ runId: "r2", replyToId: "u1", state: "queued" });
    mountChat();
    await screen.findByRole("button", { name: "Web" });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Regenerate answer" }));
    });
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/chat/sessions/s/messages/u1/regenerate", undefined),
    );
  });

  it("keeps working when the browser refuses storage", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    mountChat();
    const toggle = await screen.findByRole("button", { name: "Web" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
  });
});

describe("web sources", () => {
  const source = (over: Partial<ChatSourceDTO>): ChatSourceDTO => ({
    n: 1,
    title: "Example page",
    url: "https://news.example.com/a/b?c=1",
    favicon: null,
    snippet: "",
    kind: "web",
    ...over,
  });

  it("render as external links with their host", () => {
    render(<Sources sources={[source({})]} highlighted={null} />);
    const link = screen.getByRole("link", { name: "Example page" });
    expect(link).toHaveAttribute("href", "https://news.example.com/a/b?c=1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText("news.example.com")).toBeInTheDocument();
  });

  it("render anything but http(s) as plain text", () => {
    render(
      <Sources
        sources={[
          source({ n: 1, title: "Script", url: "javascript:alert(1)" }),
          source({ n: 2, title: "Data", url: "data:text/html,<b>x</b>" }),
        ]}
        highlighted={null}
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Script")).toBeInTheDocument();
    expect(screen.getByText("Data")).toBeInTheDocument();
  });
});

describe("the web search setting", () => {
  it("never shows a stored key, and saving without a new one keeps it", async () => {
    settings = { provider: "brave", baseUrl: null, hasKey: true, source: "tenant" };
    const put = vi.spyOn(api, "put").mockImplementation(async () => settings);
    wrap(<WebSearchSetting />);
    const key = await screen.findByLabelText("API key");
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("type", "password");
    expect(key).toHaveAttribute("placeholder", "saved");
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/settings/web-search", { provider: "brave" }),
    );
  });

  it("sends a new key once, then shows it as saved", async () => {
    settings = { provider: null, baseUrl: null, hasKey: false, source: null };
    const put = vi.spyOn(api, "put").mockImplementation(async () => {
      settings = { provider: "tavily", baseUrl: null, hasKey: true, source: "tenant" };
      return settings;
    });
    wrap(<WebSearchSetting />);
    const select = await screen.findByLabelText("Provider");
    await waitFor(() => expect(select).toBeEnabled());
    expect(select).toHaveValue("off");
    fireEvent.change(select, { target: { value: "tavily" } });
    const key = screen.getByLabelText("API key");
    expect(key).toHaveAttribute("placeholder", "");
    fireEvent.change(key, { target: { value: "tvly-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/settings/web-search", {
        provider: "tavily",
        apiKey: "tvly-secret",
      }),
    );
    await waitFor(() => expect(screen.getByLabelText("API key")).toHaveValue(""));
    expect(screen.getByLabelText("API key")).toHaveAttribute("placeholder", "saved");
  });

  it("asks SearXNG for its address", async () => {
    const put = vi.spyOn(api, "put").mockImplementation(async (_path, body) => ({
      ...(body as object),
      hasKey: false,
      source: "tenant",
    }));
    wrap(<WebSearchSetting />);
    const select = await screen.findByLabelText("Provider");
    await waitFor(() => expect(select).toBeEnabled());
    expect(screen.queryByLabelText("API key")).toBeNull();
    fireEvent.change(select, { target: { value: "searxng" } });
    fireEvent.change(screen.getByLabelText("SearXNG address"), {
      target: { value: " https://search.example.org " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/settings/web-search", {
        provider: "searxng",
        baseUrl: "https://search.example.org",
      }),
    );
  });

  it("says when the platform provides search", async () => {
    settings = { provider: "searxng", baseUrl: null, hasKey: false, source: "platform" };
    wrap(<WebSearchSetting />);
    expect(
      await screen.findByText(
        "Provided by your oc8 platform. Choose a provider to override, or switch it off.",
      ),
    ).toBeInTheDocument();
  });

  it("is read-only without settings:manage", async () => {
    may.manage = false;
    settings = { provider: "brave", baseUrl: null, hasKey: true, source: "tenant" };
    wrap(<WebSearchSetting />);
    expect(await screen.findByLabelText("API key")).toBeDisabled();
    expect(screen.getByLabelText("Provider")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("words a refused save", async () => {
    const { toast } = await import("sonner");
    vi.spyOn(api, "put").mockRejectedValue(new ApiError("This provider needs an API key.", 422));
    wrap(<WebSearchSetting />);
    const select = await screen.findByLabelText("Provider");
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.change(select, { target: { value: "brave" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("This provider needs an API key."),
    );
  });
});
