// frontend/src/test/conversation-pins.test.tsx
// C3 in the chat: the pinned bar above the composer, the + menu's context
// entries (gated by knowledge:view, hidden read-only) and "Pin to this chat".
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const granted = vi.hoisted(() => ({ current: [] as string[] }));
const session = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({
    session: session.current,
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
  useCan: () => () => true,
  useMay: () => (permission: string) => granted.current.includes(permission),
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useContextSearch: () => [{ kind: "agent", id: "a1", label: "Lennart" }],
  useContextSearchResults: () => ({
    items: [{ kind: "agent", id: "a1", label: "Lennart" }],
    isLoading: false,
  }),
  useSessionModels: () => ({ data: [] }),
  useSetSessionModel: () => ({ mutate: vi.fn() }),
}));

import { Conversation } from "@/components/conversation";
import { api } from "@/lib/api";
import { conversationKeys } from "@/lib/conversation-api";

function detail(overrides: Record<string, unknown> = {}) {
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
    pinnedRefs: [{ kind: "knowledge_base", id: "kb1", label: "Prices", available: true }],
    projectId: null,
    pinned: false,
    ...overrides,
  };
}

function mount() {
  const qc = new QueryClient();
  // The pin writer builds each PUT from the cached session.
  qc.setQueryData(conversationKeys.session("s"), session.current);
  render(
    <QueryClientProvider client={qc}>
      <Conversation agentId="a" sessionId="s" onSessionChange={vi.fn()} variant="full" />
    </QueryClientProvider>,
  );
}

function openPlus() {
  fireEvent.click(screen.getByRole("button", { name: /Add/ }));
}

describe("Conversation pins", () => {
  beforeEach(() => {
    vi.spyOn(api, "get").mockResolvedValue([]);
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows the pinned bar and offers every context source with knowledge:view", () => {
    granted.current = ["knowledge:view"];
    session.current = detail();
    mount();
    expect(screen.getByRole("list", { name: "Pinned" })).toHaveTextContent("Prices");
    openPlus();
    const menu = screen.getByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: /Choose from data source/ })).toBeVisible();
    expect(within(menu).getByRole("menuitem", { name: /Knowledge base/ })).toBeVisible();
    expect(within(menu).getByRole("menuitem", { name: /oc8 object/ })).toBeVisible();
  });

  it("hides data source and knowledge base without knowledge:view", () => {
    granted.current = [];
    session.current = detail();
    mount();
    openPlus();
    const menu = screen.getByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: /data source/ })).toBeNull();
    expect(within(menu).queryByRole("menuitem", { name: /Knowledge base/ })).toBeNull();
    expect(within(menu).getByRole("menuitem", { name: /oc8 object/ })).toBeVisible();
  });

  it("shows pins without remove buttons in a read-only view", () => {
    granted.current = ["knowledge:view"];
    session.current = detail({ readOnly: true });
    mount();
    expect(screen.getByRole("list", { name: "Pinned" })).toHaveTextContent("Prices");
    expect(screen.queryByRole("button", { name: /Unpin/ })).toBeNull();
  });

  it("attaches an oc8 object to the message and pins it to the chat", async () => {
    granted.current = [];
    session.current = detail();
    const put = vi.spyOn(api, "put").mockResolvedValue(
      detail({
        pinnedRefs: [
          { kind: "knowledge_base", id: "kb1", label: "Prices", available: true },
          { kind: "agent", id: "a1", label: "Lennart", available: true },
        ],
      }),
    );
    mount();
    openPlus();
    fireEvent.click(screen.getByRole("menuitem", { name: /oc8 object/ }));
    fireEvent.click(await screen.findByRole("option", { name: /Lennart/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Pin Lennart to this chat" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/chat/sessions/s/pins", {
        refs: [
          { kind: "knowledge_base", id: "kb1" },
          { kind: "agent", id: "a1" },
        ],
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Pin Lennart to this chat" })).toBeNull(),
    );
  });

  it("returns focus to the message box when a picker closes", async () => {
    granted.current = [];
    session.current = detail();
    mount();
    openPlus();
    fireEvent.click(screen.getByRole("menuitem", { name: /oc8 object/ }));
    const search = await screen.findByRole("combobox", { name: "Search" });
    fireEvent.keyDown(search, { key: "Escape" });
    await waitFor(() => expect(screen.getByRole("textbox", { name: /Message/ })).toHaveFocus());
  });
});
