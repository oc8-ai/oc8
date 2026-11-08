import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  canMock,
  assistantMock,
  authMock,
  sessionsMock,
  createSessionMock,
  messagesMock,
  sendMessageMock,
  renameSessionMock,
  deleteSessionMock,
  proposalsMock,
  applyProposalMock,
  rejectProposalMock,
  runActivityMock,
  liveStatusMock,
  agentsMock,
  departmentsMock,
  mayMock,
  budgetStatusMock,
  knowledgeBasesMock,
  chatModesMock,
} = vi.hoisted(() => ({
  canMock: vi.fn(() => true),
  assistantMock: vi.fn(),
  authMock: vi.fn(),
  sessionsMock: vi.fn(),
  createSessionMock: vi.fn(),
  messagesMock: vi.fn(),
  sendMessageMock: vi.fn(),
  renameSessionMock: vi.fn(),
  deleteSessionMock: vi.fn(),
  proposalsMock: vi.fn(),
  applyProposalMock: vi.fn(),
  rejectProposalMock: vi.fn(),
  runActivityMock: vi.fn(),
  liveStatusMock: vi.fn(),
  agentsMock: vi.fn(),
  departmentsMock: vi.fn(),
  mayMock: vi.fn(() => true),
  budgetStatusMock: vi.fn(),
  knowledgeBasesMock: vi.fn(),
  chatModesMock: vi.fn(),
}));

vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useCan: () => canMock, useMay: () => mayMock };
});

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useAssistant: () => assistantMock(),
    useAuth: () => authMock(),
    useCopilotProposals: (options?: unknown) => proposalsMock(options),
    useApplyCopilotProposal: () => ({ mutate: applyProposalMock, isPending: false }),
    useRejectCopilotProposal: () => ({ mutate: rejectProposalMock, isPending: false }),
    useAgents: (params?: unknown) => agentsMock(params),
    useDepartments: (params?: unknown) => departmentsMock(params),
    useBudgetStatus: (departmentId: string | null, options?: unknown) =>
      budgetStatusMock(departmentId, options),
    useKnowledgeBases: (params?: unknown) => knowledgeBasesMock(params),
  };
});

vi.mock("@/lib/chat-commands", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/chat-commands")>();
  return { ...actual, useChatModes: () => chatModesMock() };
});

vi.mock("@/lib/hooks-chat", () => ({
  // Forwarding the real args lets a test answer per agent or session via
  // mockImplementation.
  useChatSessions: (agentId?: string) => sessionsMock(agentId),
  useCreateChatSession: () => ({ mutate: createSessionMock, isPending: false }),
  useChatMessages: (sessionId: string | null) => messagesMock(sessionId),
  useSendChatMessage: () => ({ mutate: sendMessageMock, isPending: false }),
  useRenameChatSession: () => ({ mutate: renameSessionMock, isPending: false }),
  useDeleteChatSession: () => ({ mutate: deleteSessionMock, isPending: false }),
  useCopilotRunActivity: (sessionId: string | null, runId: string | null) =>
    runActivityMock(sessionId, runId),
}));

vi.mock("@/lib/live/provider", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/live/provider")>();
  return { ...actual, useLiveConnectionStatus: () => liveStatusMock() };
});

// The header's "Open Copilot" link needs a router; the dock tests mount no router.
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
    <a href={to}>{children}</a>
  ),
}));
vi.mock("@/lib/hooks-copilot", () => ({
  useCopilotProfile: () => ({
    data: {
      displayName: "Copilot",
      avatar: { shape: "round", color: "indigo" },
      pausedAt: null,
      status: "ready",
      activeCount: 0,
      agentId: "a",
    },
  }),
  usePauseCopilot: () => ({ mutate: vi.fn(), isPending: false }),
  useResumeCopilot: () => ({ mutate: vi.fn(), isPending: false }),
}));

// The one chat is tested on its own (conversation-*.test.tsx); here it is a
// stand-in that exposes what the dock hands it.
vi.mock("@/components/conversation", () => ({
  Conversation: (p: {
    agentId: string;
    sessionId: string | null;
    onSessionChange: (id: string) => void;
    variant: string;
    draft?: string;
    onDraftChange?: (text: string) => void;
    placeholder?: string;
    emptyState?: React.ReactNode;
    composerAbove?: React.ReactNode;
    agentItems?: Array<{ id: string; label: string; hint?: string; blocked?: string }>;
    onAddressAgent?: (agentId: string) => void;
  }) => (
    <div
      data-testid="conversation"
      data-agent={p.agentId}
      data-session={p.sessionId ?? ""}
      data-variant={p.variant}
    >
      {p.sessionId === null && p.emptyState}
      {p.composerAbove}
      {p.draft?.includes("@") && (
        <ul role="listbox" aria-label="Agents">
          {(p.agentItems ?? []).map((item) => (
            <li
              key={item.id}
              role="option"
              aria-selected={false}
              aria-disabled={item.blocked ? "true" : undefined}
              onClick={() => !item.blocked && p.onAddressAgent?.(item.id)}
            >
              <span>{item.label}</span>
              {item.hint && <span>{item.hint}</span>}
              {item.blocked && <span>{item.blocked}</span>}
            </li>
          ))}
        </ul>
      )}
      <input
        placeholder={p.placeholder}
        value={p.draft ?? ""}
        onChange={(e) => p.onDraftChange?.(e.target.value)}
      />
      <button type="button" onClick={() => p.onSessionChange(`created-${p.agentId}`)}>
        mock create session
      </button>
    </div>
  ),
}));

import { CopilotDock } from "@/components/copilot-dock";

function renderDock() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(
    <QueryClientProvider client={qc}>
      <CopilotDock />
    </QueryClientProvider>,
  );
  return { ...result, qc };
}

function openDock() {
  fireEvent.click(screen.getByRole("button", { name: /oc8 copilot/i }));
}

describe("CopilotDock", () => {
  beforeEach(() => {
    // Tabs are now persisted to localStorage (scoped by member id) -- clear
    // it so one test's open tabs can't leak into the next test's initial
    // render. useAuth resolves synchronously to a fixed member here so every
    // test's tabs/bootstrap logic activates on the very first render, same
    // as before memberId depended on useAuth actually resolving; the one
    // test below that needs the real pending-then-resolved race overrides
    // this itself.
    localStorage.clear();
    canMock.mockReset();
    canMock.mockImplementation(() => true);
    assistantMock.mockReset();
    assistantMock.mockReturnValue({ data: { agentId: "assistant-1" } });
    authMock.mockReset();
    authMock.mockReturnValue({ data: { memberId: "member-1" } });
    sessionsMock.mockReset();
    sessionsMock.mockReturnValue({ data: [] });
    createSessionMock.mockReset();
    messagesMock.mockReset();
    messagesMock.mockReturnValue({ data: undefined });
    sendMessageMock.mockReset();
    renameSessionMock.mockReset();
    deleteSessionMock.mockReset();
    proposalsMock.mockReset();
    proposalsMock.mockReturnValue({ data: [] });
    applyProposalMock.mockReset();
    rejectProposalMock.mockReset();
    runActivityMock.mockReset();
    runActivityMock.mockReturnValue({ data: null });
    liveStatusMock.mockReset();
    liveStatusMock.mockReturnValue("connected");
    agentsMock.mockReset();
    agentsMock.mockReturnValue({
      data: {
        items: [{ id: "sina", name: "Sina", role: "Support Agent", departmentId: "dept-1" }],
        totalCount: 1,
      },
    });
    departmentsMock.mockReset();
    departmentsMock.mockReturnValue({
      data: { items: [{ id: "dept-1", name: "Support" }], totalCount: 1 },
    });
    mayMock.mockReset();
    mayMock.mockImplementation(() => true);
    budgetStatusMock.mockReset();
    budgetStatusMock.mockReturnValue({
      data: {
        scope: "tenant",
        departmentId: null,
        softLimitTokens: null,
        hardLimitTokens: 100000,
        currentTokens: 4200,
        softExceeded: false,
        hardExceeded: false,
      },
    });
    knowledgeBasesMock.mockReset();
    knowledgeBasesMock.mockReturnValue({ data: { items: [], totalCount: 0 } });
    chatModesMock.mockReset();
    chatModesMock.mockReturnValue({
      data: [
        {
          key: "plan",
          summary: "Work out the steps and show them. Changes nothing.",
          allowsTools: true,
          allowsWrites: false,
        },
      ],
    });
  });

  it("renders nothing for a caller without copilot:use, and never even calls the chat-pipeline hooks", () => {
    canMock.mockImplementation(() => false);
    renderDock();
    expect(screen.queryByRole("button", { name: /oc8 copilot/i })).not.toBeInTheDocument();
    // The permission check has to happen BEFORE useAssistant/useChatSessions
    // are called, not just before their result is rendered -- otherwise
    // every signed-in user fires a GET /assistant + GET /chat/sessions (a
    // 403 for anyone without copilot:use) on every page load.
    expect(assistantMock).not.toHaveBeenCalled();
    expect(sessionsMock).not.toHaveBeenCalled();
  });

  it("shows a greeting and no session yet until the caller sends a first message", () => {
    renderDock();
    openDock();
    expect(screen.getByText(/i know your agents, departments and guardrails/i)).toBeInTheDocument();
    expect(sendMessageMock).not.toHaveBeenCalled();
    expect(createSessionMock).not.toHaveBeenCalled();
  });

  it("defaults to the Assistant's most recent existing session instead of starting a new one", () => {
    sessionsMock.mockReturnValue({
      data: [{ id: "s1", agentId: "assistant-1", title: "", createdAt: "t", lastMessageAt: null }],
    });
    renderDock();
    openDock();
    const conversation = screen.getByTestId("conversation");
    expect(conversation).toHaveAttribute("data-session", "s1");
    expect(conversation).toHaveAttribute("data-agent", "assistant-1");
    expect(conversation).toHaveAttribute("data-variant", "compact");
  });

  // --- propose_change proposals are reviewable in the dock ---------------
  //
  // The Assistant's propose_change tool may only ever DRAFT. Between the dock
  // rewrite and this, nothing in the app rendered a proposal: the apply/reject
  // mutations existed with zero call sites, so every structural change the
  // Assistant proposed -- on the web or over Telegram -- sat in the database
  // with no way for anyone to answer it.

  const draft = {
    id: "p1",
    status: "draft",
    revision: 1,
    operations: [{ label: "agent.mission.set", references: { agentId: "a-1" } }],
  };

  it("renders a pending proposal with its operations", () => {
    proposalsMock.mockReturnValue({ data: [draft] });
    renderDock();
    openDock();
    expect(screen.getByRole("region", { name: /pending proposals/i })).toBeInTheDocument();
    expect(screen.getByText("agent.mission.set")).toBeInTheDocument();
    expect(screen.getByText("a-1")).toBeInTheDocument();
  });

  it("Apply calls the apply endpoint for that proposal and the card stops showing", () => {
    proposalsMock.mockReturnValue({ data: [draft] });
    renderDock();
    openDock();
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    expect(applyProposalMock).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ onError: expect.any(Function) }),
    );
    expect(rejectProposalMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: /pending proposals/i })).not.toBeInTheDocument();
  });

  it("Reject calls the reject endpoint for that proposal and the card stops showing", () => {
    proposalsMock.mockReturnValue({ data: [draft] });
    renderDock();
    openDock();
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));
    expect(rejectProposalMock).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ onError: expect.any(Function) }),
    );
    expect(applyProposalMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: /pending proposals/i })).not.toBeInTheDocument();
  });

  // A failed apply/reject is the case the optimistic mark gets WRONG: the card
  // was hidden the moment the button was pressed and never came back, so the
  // reader was left believing a structural change went through while the
  // proposal was in fact still sitting there as `draft`.

  it("a failed Apply puts the still-draft card back and says so", () => {
    proposalsMock.mockReturnValue({ data: [draft] });
    applyProposalMock.mockImplementation((_id: string, options: { onError: () => void }) => {
      options.onError();
    });
    renderDock();
    openDock();
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

    expect(screen.getByRole("region", { name: /pending proposals/i })).toBeInTheDocument();
    expect(screen.getByText("agent.mission.set")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^apply$/i })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/could not be answered/i);
  });

  it("a failed Reject puts the still-draft card back and says so", () => {
    proposalsMock.mockReturnValue({ data: [draft] });
    rejectProposalMock.mockImplementation((_id: string, options: { onError: () => void }) => {
      options.onError();
    });
    renderDock();
    openDock();
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));

    expect(screen.getByRole("region", { name: /pending proposals/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^reject$/i })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/could not be answered/i);
  });

  it("a successful answer after a failed one clears the notice", () => {
    proposalsMock.mockReturnValue({ data: [draft] });
    applyProposalMock.mockImplementationOnce((_id: string, options: { onError: () => void }) => {
      options.onError();
    });
    renderDock();
    openDock();
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    // The second press succeeds -- the once-mock is spent, so nothing calls
    // onError -- and the stale notice must not outlive it.
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("an already applied or rejected proposal is never offered for review", () => {
    proposalsMock.mockReturnValue({
      data: [
        { ...draft, id: "p-applied", status: "applied" },
        { ...draft, id: "p-rejected", status: "rejected" },
      ],
    });
    renderDock();
    openDock();
    expect(screen.queryByRole("region", { name: /pending proposals/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^apply$/i })).not.toBeInTheDocument();
  });

  it("shows no proposals section at all when nothing is waiting", () => {
    renderDock();
    openDock();
    expect(screen.queryByRole("region", { name: /pending proposals/i })).not.toBeInTheDocument();
  });

  it("does not fetch or poll pending proposals for a caller who only holds copilot:use", () => {
    canMock.mockImplementation((p: string) => p === "copilot:use");
    renderDock();
    openDock();
    expect(proposalsMock).toHaveBeenCalledWith(expect.objectContaining({ enabled: false }));
  });

  it("shows pending proposals but hides Apply/Reject for a caller without copilot:manage", () => {
    canMock.mockImplementation((p: string) => p === "copilot:use" || p === "copilot:view");
    proposalsMock.mockReturnValue({ data: [draft] });
    renderDock();
    openDock();
    expect(screen.getByRole("region", { name: /pending proposals/i })).toBeInTheDocument();
    expect(screen.getByText("agent.mission.set")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^apply$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^reject$/i })).not.toBeInTheDocument();
  });

  it("closing and reopening the dock keeps the same session (no reset on remount-free toggle)", () => {
    sessionsMock.mockReturnValue({
      data: [{ id: "s1", agentId: "assistant-1", title: "", createdAt: "t", lastMessageAt: null }],
    });
    renderDock();
    openDock();
    expect(screen.getByTestId("conversation")).toHaveAttribute("data-session", "s1");
    fireEvent.click(screen.getByRole("button", { name: /oc8 copilot/i }));
    expect(screen.queryByTestId("conversation")).not.toBeInTheDocument();
    openDock();
    expect(screen.getByTestId("conversation")).toHaveAttribute("data-session", "s1");
  });

  it("shows the session picker once more than one session exists, and switches on selection", () => {
    sessionsMock.mockReturnValue({
      data: [
        {
          id: "s1",
          agentId: "assistant-1",
          title: "Erste Frage",
          createdAt: "2026-01-01T00:00:00Z",
          lastMessageAt: null,
        },
        {
          id: "s2",
          agentId: "assistant-1",
          title: "Zweite Frage",
          createdAt: "2026-01-02T00:00:00Z",
          lastMessageAt: null,
        },
      ],
    });
    renderDock();
    openDock();
    // The picker's trigger renders the current session's title in a
    // `span.truncate` -- scope to that so this doesn't also match the menu
    // item once the dropdown is open (its own row also carries the title).
    expect(screen.getByText("Erste Frage", { selector: "span.truncate" })).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByText("Erste Frage", { selector: "span.truncate" }), {
      button: 0,
    });
    fireEvent.click(screen.getByText("Zweite Frage"));
    // The picker's trigger now shows the newly-selected session's title.
    expect(screen.getByText("Zweite Frage", { selector: "span.truncate" })).toBeInTheDocument();
    expect(
      screen.queryByText("Erste Frage", { selector: "span.truncate" }),
    ).not.toBeInTheDocument();
  });

  it("a new chat button resets to the lazy-creation state even when other sessions exist", () => {
    sessionsMock.mockReturnValue({
      data: [
        {
          id: "s1",
          agentId: "assistant-1",
          title: "Erste Frage",
          createdAt: "t",
          lastMessageAt: null,
        },
      ],
    });
    renderDock();
    openDock();
    expect(screen.getByTestId("conversation")).toHaveAttribute("data-session", "s1");
    fireEvent.click(screen.getByRole("button", { name: /new chat/i }));
    // Back to a blank chat that creates its session on the first send, not
    // clobbered by the bootstrap effect that auto-selects the existing one.
    expect(screen.getByTestId("conversation")).toHaveAttribute("data-session", "");
    expect(screen.getByText(/i know your agents, departments and guardrails/i)).toBeInTheDocument();
  });

  it("a new chat button still works right after a tenant's very first session is created lazily", () => {
    renderDock();
    openDock();
    // The conversation creates its session on the first send and reports it.
    fireEvent.click(screen.getByRole("button", { name: "mock create session" }));
    expect(screen.getByTestId("conversation")).toHaveAttribute(
      "data-session",
      "created-assistant-1",
    );
    fireEvent.click(screen.getByRole("button", { name: /new chat/i }));
    expect(screen.getByTestId("conversation")).toHaveAttribute("data-session", "");
    expect(document.querySelectorAll("[data-copilot-tab]")).toHaveLength(1);
  });

  it("opens a new blank tab without disturbing the currently active tab's session", () => {
    sessionsMock.mockReturnValue({
      data: [
        {
          id: "s1",
          agentId: "assistant-1",
          title: "Erste Frage",
          createdAt: "t",
          lastMessageAt: null,
        },
      ],
    });
    renderDock();
    openDock();
    expect(screen.getByTestId("conversation")).toHaveAttribute("data-session", "s1");

    fireEvent.click(screen.getByRole("button", { name: /^new tab$/i }));
    // Only the active tab's conversation is mounted, and the new one is blank.
    expect(screen.getAllByTestId("conversation")).toHaveLength(1);
    expect(screen.getByTestId("conversation")).toHaveAttribute("data-session", "");

    fireEvent.click(screen.getByRole("button", { name: /erste frage tab/i }));
    expect(screen.getByTestId("conversation")).toHaveAttribute("data-session", "s1");
  });

  it("switching tabs preserves each tab's own composer draft text", () => {
    renderDock();
    openDock();
    fireEvent.change(screen.getByPlaceholderText(/configure or ask oc8/i), {
      target: { value: "Draft in tab one" },
    });

    fireEvent.click(screen.getByRole("button", { name: /^new tab$/i }));
    fireEvent.change(screen.getByPlaceholderText(/configure or ask oc8/i), {
      target: { value: "Draft in tab two" },
    });

    // Both tabs are still session-less, so both pills fall back to the same
    // "untitled" label -- index into DOM order (tab creation order) rather
    // than by name to tell them apart.
    const tabButtons = screen.getAllByRole("button", { name: /untitled chat tab/i });
    expect(tabButtons).toHaveLength(2);

    fireEvent.click(tabButtons[0]);
    expect(screen.getByPlaceholderText(/configure or ask oc8/i)).toHaveValue("Draft in tab one");

    fireEvent.click(tabButtons[1]);
    expect(screen.getByPlaceholderText(/configure or ask oc8/i)).toHaveValue("Draft in tab two");
  });

  it("selecting a session via ChatSessionPicker focuses its existing tab instead of duplicating it", () => {
    sessionsMock.mockReturnValue({
      data: [
        {
          id: "s1",
          agentId: "assistant-1",
          title: "Erste Frage",
          createdAt: "2026-01-01T00:00:00Z",
          lastMessageAt: null,
        },
        {
          id: "s2",
          agentId: "assistant-1",
          title: "Zweite Frage",
          createdAt: "2026-01-02T00:00:00Z",
          lastMessageAt: null,
        },
      ],
    });
    renderDock();
    openDock();
    // Bootstrap opens a first tab on the most recent session, s1.
    expect(screen.getByText("Erste Frage", { selector: "span.truncate" })).toBeInTheDocument();
    // Counted via the DOM structure (not getByRole) since the tab pills sit
    // outside the still-open dropdown's content and Radix marks the rest of
    // the page aria-hidden while it's open -- getByRole would incorrectly
    // see zero tabs whenever the picker's dropdown is open.
    expect(document.querySelectorAll("[data-copilot-tab]")).toHaveLength(1);

    // Pick s2 via the picker -- opens it into a second tab.
    fireEvent.pointerDown(screen.getByText("Erste Frage", { selector: "span.truncate" }), {
      button: 0,
    });
    fireEvent.click(screen.getByText("Zweite Frage"));
    expect(screen.getByText("Zweite Frage", { selector: "span.truncate" })).toBeInTheDocument();
    expect(document.querySelectorAll("[data-copilot-tab]")).toHaveLength(2);

    // Pick s1 again via the picker -- must focus the FIRST tab, not open a
    // third one.
    fireEvent.pointerDown(screen.getByText("Zweite Frage", { selector: "span.truncate" }), {
      button: 0,
    });
    fireEvent.click(screen.getByText("Erste Frage", { selector: "button" }));

    expect(screen.getByText("Erste Frage", { selector: "span.truncate" })).toBeInTheDocument();
    expect(document.querySelectorAll("[data-copilot-tab]")).toHaveLength(2);
  });

  it("closing a tab removes it and activates the previous tab", () => {
    renderDock();
    openDock();
    // Tab 1 (bootstrapped, blank). Open two more.
    fireEvent.click(screen.getByRole("button", { name: /^new tab$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^new tab$/i }));
    let tabButtons = screen.getAllByRole("button", { name: /untitled chat tab/i });
    expect(tabButtons).toHaveLength(3);

    // Tab 3 is active; close it via its own "close" button.
    const closeButtons = screen.getAllByRole("button", { name: /^close untitled chat$/i });
    fireEvent.click(closeButtons[closeButtons.length - 1]);

    tabButtons = screen.getAllByRole("button", { name: /untitled chat tab/i });
    expect(tabButtons).toHaveLength(2);
    // The previous tab (tab 2) is now the active one -- its composer is the
    // one rendered.
    expect(screen.getByPlaceholderText(/configure or ask oc8/i)).toBeInTheDocument();
  });

  it("persists open tabs across a remount, scoped to the current member", () => {
    sessionsMock.mockReturnValue({
      data: [
        {
          id: "s1",
          agentId: "assistant-1",
          title: "Erste Frage",
          createdAt: "t",
          lastMessageAt: null,
        },
      ],
    });
    const { unmount } = renderDock();
    openDock();
    fireEvent.click(screen.getByRole("button", { name: /^new tab$/i }));
    expect(screen.getAllByRole("button", { name: /frage tab|untitled chat tab/i })).toHaveLength(2);

    unmount();

    expect(localStorage.getItem("oc8-copilot-tabs-member-1")).not.toBeNull();

    renderDock();
    openDock();
    expect(screen.getAllByRole("button", { name: /frage tab|untitled chat tab/i })).toHaveLength(2);
  });

  it("does not clobber a real member's persisted tabs while useAuth is still resolving", () => {
    // Pre-seed localStorage as if this member already had two tabs open from
    // a previous visit.
    localStorage.setItem(
      "oc8-copilot-tabs-member-1",
      JSON.stringify([
        { uiId: "existing-1", sessionId: null },
        { uiId: "existing-2", sessionId: null },
      ]),
    );

    // Simulate the exact race the bug depended on: useAuth's query is still
    // pending (`data: undefined`) on the first render, and only resolves to
    // the real member afterwards. authMock is a plain vi.fn() rather than a
    // real async query, so changing its return value alone doesn't trigger a
    // re-render -- the toggle clicks below force CopilotDockPanel to
    // re-render and read the new value, the same way a real query settling
    // would.
    authMock.mockReturnValue({ data: undefined });

    renderDock();
    openDock();
    // While useAuth is still pending, memberId is unknown -- no tabs should
    // have loaded or bootstrapped yet (no composer for a tab).
    expect(screen.queryByPlaceholderText(/configure or ask oc8/i)).not.toBeInTheDocument();

    authMock.mockReturnValue({ data: { memberId: "member-1" } });
    // Force a re-render so the new mock return value is actually read.
    fireEvent.click(screen.getByRole("button", { name: /oc8 copilot/i }));
    fireEvent.click(screen.getByRole("button", { name: /oc8 copilot/i }));

    expect(JSON.parse(localStorage.getItem("oc8-copilot-tabs-member-1") ?? "[]")).toHaveLength(2);
  });

  it("preserves an unsent draft across closing and reopening the dock", () => {
    renderDock();
    openDock();
    fireEvent.change(screen.getByPlaceholderText(/configure or ask oc8/i), {
      target: { value: "unsent draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: /oc8 copilot/i })); // close
    expect(screen.queryByPlaceholderText(/configure or ask oc8/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /oc8 copilot/i })); // reopen
    expect(screen.getByPlaceholderText(/configure or ask oc8/i)).toHaveValue("unsent draft");
  });

  it("falls back to a working composer when useAuth fails outright, instead of staying blocked forever", () => {
    authMock.mockReturnValue({ data: undefined, isError: true });
    renderDock();
    openDock();
    expect(screen.getByPlaceholderText(/configure or ask oc8/i)).toBeInTheDocument();
  });

  it("shows a reconnecting notice while the WS connection is down", () => {
    liveStatusMock.mockReturnValue("disconnected");
    renderDock();
    openDock();
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument();
  });

  it("hides the reconnecting notice once the WS connection is back", () => {
    liveStatusMock.mockReturnValue("connected");
    renderDock();
    openDock();
    expect(screen.queryByText(/reconnecting/i)).not.toBeInTheDocument();
  });

  // --- Task 15: @ agent switching, / commands and /budget in the dock ------

  describe("the dock composer's sigils", () => {
    it('typing "@" lists agents with their role and department', () => {
      renderDock();
      openDock();
      const textarea = screen.getByPlaceholderText(/configure or ask oc8/i);
      fireEvent.change(textarea, { target: { value: "@", selectionStart: 1 } });
      expect(screen.getByRole("listbox", { name: /agents/i })).toBeInTheDocument();
      expect(screen.getByText("Sina")).toBeInTheDocument();
      expect(screen.getByText("Support Agent · Support")).toBeInTheDocument();
    });

    it("picking an agent switches who the conversation goes to and says so", () => {
      renderDock();
      openDock();
      const textarea = screen.getByPlaceholderText(/configure or ask oc8/i);
      fireEvent.change(textarea, { target: { value: "@", selectionStart: 1 } });
      fireEvent.click(screen.getByRole("option", { name: /Sina/i }));

      expect(screen.getByText(/goes to Sina — not the copilot/i)).toBeInTheDocument();
      expect(createSessionMock).not.toHaveBeenCalled();
    });

    it("a caller without run:start sees the agent but cannot pick it", () => {
      mayMock.mockImplementation(() => false);
      renderDock();
      openDock();
      const textarea = screen.getByPlaceholderText(/configure or ask oc8/i);
      fireEvent.change(textarea, { target: { value: "@", selectionStart: 1 } });

      const option = screen.getByRole("option", { name: /Sina/i });
      expect(option).toHaveAttribute("aria-disabled", "true");
      expect(screen.getByText(/you may not start runs for another agent/i)).toBeInTheDocument();

      fireEvent.click(option);
      expect(screen.queryByText(/goes to Sina/i)).not.toBeInTheDocument();
    });

    it("after a switch the tab talks to the NEW agent in a fresh conversation", () => {
      sessionsMock.mockReturnValue({
        data: [
          { id: "s1", agentId: "assistant-1", title: "", createdAt: "t", lastMessageAt: null },
        ],
      });
      renderDock();
      openDock();
      const textarea = screen.getByPlaceholderText(/configure or ask oc8/i);
      fireEvent.change(textarea, { target: { value: "@", selectionStart: 1 } });
      fireEvent.click(screen.getByRole("option", { name: /Sina/i }));

      const conversation = screen.getByTestId("conversation");
      expect(conversation).toHaveAttribute("data-agent", "sina");
      expect(conversation).toHaveAttribute("data-session", "");
    });
  });
});
