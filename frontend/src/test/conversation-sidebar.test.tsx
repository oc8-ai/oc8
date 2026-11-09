import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({
  pin: vi.fn(),
  move: vi.fn(),
  rename: vi.fn(),
  del: vi.fn(),
  delProject: vi.fn(),
  narrow: false,
  sessions: [] as Array<Record<string, unknown>>,
  projects: [] as Array<Record<string, unknown>>,
  loading: false,
}));

vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/hooks/use-mobile", () => ({ useIsNarrow: () => m.narrow, useIsMobile: () => m.narrow }));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useMay: () => () => true,
}));
vi.mock("@/lib/hooks-chat", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks-chat")>()),
  useChatSessions: () => ({ data: m.sessions, isLoading: m.loading }),
  useRenameChatSession: () => ({ mutate: m.rename }),
  useDeleteChatSession: () => ({ mutate: m.del }),
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useChatProjects: () => ({ data: m.projects }),
  useSetSessionPinned: () => ({ mutate: m.pin }),
  useSetSessionProject: () => ({ mutate: m.move }),
  useDeleteProject: () => ({ mutate: m.delProject }),
  useSaveProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}));

import { toast } from "sonner";
import { chatKeys } from "@/lib/hooks-chat";
import { CopilotSidebar, groupSessions } from "@/components/conversation/copilot-sidebar";
import { EditionCompositionProvider } from "@/edition/composition";
import type { FrontendEditionExtension } from "@/edition/contracts";

const session = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  agentId: "x",
  title: id,
  createdAt: "",
  lastMessageAt: "2026-11-08T08:00:00Z",
  pinned: false,
  projectId: null,
  ...over,
});

function mount(
  props: Partial<React.ComponentProps<typeof CopilotSidebar>> = {},
  extensions: readonly FrontendEditionExtension[] = [],
) {
  const onSelect = vi.fn();
  const ui = (
    <QueryClientProvider client={new QueryClient()}>
      <EditionCompositionProvider extensions={extensions}>
        <CopilotSidebar
          agentId="x"
          sessionId={null}
          onSelect={onSelect}
          collapsed={false}
          onToggle={vi.fn()}
          {...props}
        />
      </EditionCompositionProvider>
    </QueryClientProvider>
  );
  return { onSelect, ...render(ui) };
}

function openMenu(name: string) {
  fireEvent.keyDown(screen.getByRole("button", { name }), { key: "Enter" });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers({ toFake: ["Date"], now: new Date(2026, 10, 8, 12, 0) });
  m.narrow = false;
  m.loading = false;
  m.projects = [{ id: "p1", name: "Q4 pricing", instructions: "", pinnedRefs: [], createdAt: "" }];
  m.sessions = [
    session("Pinned one", {
      id: "a",
      title: "Pinned one",
      lastMessageAt: "2026-11-01T10:00:00Z",
      pinned: true,
    }),
    session("Today chat", { id: "b", title: "Today chat" }),
    session("In project", {
      id: "c",
      title: "In project",
      lastMessageAt: "2026-11-02T08:00:00Z",
      projectId: "p1",
    }),
  ];
});

afterEach(() => vi.useRealTimers());

describe("groupSessions", () => {
  const mk = (id: string, lastMessageAt: string, over: Record<string, unknown> = {}) =>
    session(id, { lastMessageAt, ...over }) as never;

  it("groups by pinned, project and date", () => {
    const groups = groupSessions(
      [
        mk("a", "2026-11-01T10:00:00Z", { pinned: true }),
        mk("b", "2026-11-08T08:00:00Z"),
        mk("c", "2026-11-05T08:00:00Z"),
        mk("d", "2026-10-01T08:00:00Z"),
        mk("e", "2026-11-02T08:00:00Z", { projectId: "p1" }),
      ],
      new Date("2026-11-08T12:00:00Z"),
    );
    expect(groups.pinned.map((s) => s.id)).toEqual(["a"]);
    expect(groups.today.map((s) => s.id)).toEqual(["b"]);
    expect(groups.week.map((s) => s.id)).toEqual(["c"]);
    expect(groups.older.map((s) => s.id)).toEqual(["d"]);
    expect(groups.byProject.get("p1")?.map((s) => s.id)).toEqual(["e"]);
  });

  it("splits Today from Previous 7 days at local midnight", () => {
    const at = (h: number, min: number, day = 8) => new Date(2026, 10, day, h, min).toISOString();
    const sessions = [
      mk("late", at(23, 50, 7)),
      mk("early", at(0, 5)),
      mk("edge", at(0, 0, 2)),
      mk("beyond", at(23, 59, 1)),
    ];
    const before = groupSessions(sessions, new Date(2026, 10, 8, 23, 59));
    expect(before.today.map((s) => s.id)).toEqual(["early"]);
    expect(before.week.map((s) => s.id)).toEqual(["late", "edge"]);
    expect(before.older.map((s) => s.id)).toEqual(["beyond"]);
    // One minute later it is the next local day: the same chat moves down.
    const after = groupSessions(sessions, new Date(2026, 10, 9, 0, 0));
    expect(after.today).toEqual([]);
    expect(after.week.map((s) => s.id)).toEqual(["early", "late"]);
    expect(after.older.map((s) => s.id)).toEqual(["edge", "beyond"]);
  });

  it("puts a chat without any message by its creation time, newest first", () => {
    const groups = groupSessions(
      [
        session("old", {
          lastMessageAt: null,
          createdAt: new Date(2026, 10, 8, 9).toISOString(),
        }) as never,
        mk("new", new Date(2026, 10, 8, 11).toISOString()),
      ],
      new Date(2026, 10, 8, 12),
    );
    expect(groups.today.map((s) => s.id)).toEqual(["new", "old"]);
  });
});

describe("Copilot sidebar", () => {
  it("is a labelled nav with a headed list per group", () => {
    mount();
    const nav = screen.getByRole("navigation", { name: "Chats" });
    expect(within(nav).getByRole("heading", { name: "Pinned" })).toBeInTheDocument();
    expect(within(nav).getByRole("heading", { name: "Today" })).toBeInTheDocument();
    expect(within(nav).getByRole("list", { name: "Today" })).toBeInTheDocument();
    expect(within(nav).getByRole("list", { name: "Q4 pricing" })).toBeInTheDocument();
    expect(within(nav).queryByRole("heading", { name: "Older" })).toBeNull();
  });

  it("selects a chat and starts a new one", () => {
    const { onSelect } = mount({ sessionId: "b" });
    fireEvent.click(screen.getByRole("button", { name: "Today chat" }));
    expect(onSelect).toHaveBeenCalledWith("b");
    expect(screen.getByRole("button", { name: "Today chat" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("pins from the row menu, reached by keyboard", () => {
    mount();
    openMenu("More: Today chat");
    fireEvent.click(screen.getByRole("menuitem", { name: "Pin" }));
    expect(m.pin).toHaveBeenCalledWith({ sessionId: "b", pinned: true }, expect.anything());
  });

  it("offers Unpin on a pinned chat", () => {
    mount();
    openMenu("More: Pinned one");
    fireEvent.click(screen.getByRole("menuitem", { name: "Unpin" }));
    expect(m.pin).toHaveBeenCalledWith({ sessionId: "a", pinned: false }, expect.anything());
  });

  it("moves a chat into a project and out of it", () => {
    mount();
    openMenu("More: Today chat");
    fireEvent.click(screen.getByRole("menuitem", { name: "Move to Q4 pricing" }));
    expect(m.move).toHaveBeenCalledWith({ sessionId: "b", projectId: "p1" }, expect.anything());
    openMenu("More: In project");
    expect(screen.getByRole("menuitem", { name: "Move to Q4 pricing" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "Remove from project" }));
    expect(m.move).toHaveBeenLastCalledWith({ sessionId: "c", projectId: null }, expect.anything());
  });

  async function startRename() {
    openMenu("More: Today chat");
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const field = await screen.findByRole("textbox", { name: "Chat name" });
    // Past the first frame: from here a blur is the member leaving the field.
    await act(() => new Promise((r) => requestAnimationFrame(() => r(null))));
    return field;
  }

  it("renames in place on Enter and cancels on Escape", async () => {
    mount();
    const field = await startRename();
    fireEvent.change(field, { target: { value: "Budget" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(m.rename).toHaveBeenCalledWith({ sessionId: "b", title: "Budget" }, expect.anything());

    m.rename.mockClear();
    const again = await startRename();
    fireEvent.change(again, { target: { value: "Nope" } });
    fireEvent.keyDown(again, { key: "Escape" });
    expect(m.rename).not.toHaveBeenCalled();
  });

  it("ignores the blur of the menu's focus return while the rename field opens", async () => {
    mount();
    openMenu("More: Today chat");
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const field = await screen.findByRole("textbox", { name: "Chat name" });
    fireEvent.change(field, { target: { value: "Budget" } });
    // Radix returning focus to the trigger blurs the field in its first frame.
    fireEvent.blur(field);
    expect(screen.getByRole("textbox", { name: "Chat name" })).toHaveFocus();
    expect(m.rename).not.toHaveBeenCalled();
    await act(() => new Promise((r) => requestAnimationFrame(() => r(null))));
    fireEvent.blur(screen.getByRole("textbox", { name: "Chat name" }));
    expect(m.rename).toHaveBeenCalledWith({ sessionId: "b", title: "Budget" }, expect.anything());
  });

  async function confirmDelete(title: string) {
    openMenu(`More: ${title}`);
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  }

  it("asks before deleting and deletes only once confirmed", async () => {
    mount();
    openMenu("More: Today chat");
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(m.del).not.toHaveBeenCalled();
    await confirmDelete("Today chat");
    await waitFor(() => expect(m.del).toHaveBeenCalledWith("b", expect.anything()));
  });

  it("deleting the open chat selects the most recent remaining one, not the list head", async () => {
    // The list is pinned first: "Pinned one" leads but is the oldest.
    const { onSelect } = mount({ sessionId: "b" });
    await confirmDelete("Today chat");
    await waitFor(() => expect(m.del).toHaveBeenCalled());
    const options = m.del.mock.calls[0][1] as { onSuccess: () => void };
    act(() => options.onSuccess());
    expect(onSelect).toHaveBeenCalledWith("c");
  });

  it("picks the next chat from the latest cache, not the list captured at render", async () => {
    const qc = new QueryClient();
    // A newer chat arrived after this render: the list shown here is stale.
    qc.setQueryData(chatKeys.sessions("x"), [
      ...m.sessions,
      session("fresh", { id: "fresh", title: "Fresh", lastMessageAt: "2026-11-08T11:00:00Z" }),
    ]);
    const onSelect = vi.fn();
    render(
      <QueryClientProvider client={qc}>
        <EditionCompositionProvider extensions={[]}>
          <CopilotSidebar
            agentId="x"
            sessionId="b"
            onSelect={onSelect}
            collapsed={false}
            onToggle={vi.fn()}
          />
        </EditionCompositionProvider>
      </QueryClientProvider>,
    );
    await confirmDelete("Today chat");
    await waitFor(() => expect(m.del).toHaveBeenCalled());
    act(() => (m.del.mock.calls[0][1] as { onSuccess: () => void }).onSuccess());
    expect(onSelect).toHaveBeenCalledWith("fresh");
  });

  it("tells the member when a write fails", async () => {
    mount();
    openMenu("More: Today chat");
    fireEvent.click(screen.getByRole("menuitem", { name: "Pin" }));
    (m.pin.mock.calls[0][1] as { onError: () => void }).onError();
    expect(toast.error).toHaveBeenCalledWith("The chat was not changed. Try again.");
    await confirmDelete("Today chat");
    await waitFor(() => expect(m.del).toHaveBeenCalled());
    (m.del.mock.calls[0][1] as { onError: () => void }).onError();
    expect(toast.error).toHaveBeenCalledWith("The chat was not deleted. Try again.");
  });

  it("deleting the only open chat starts a new one", async () => {
    m.sessions = [session("only", { id: "only", title: "Only chat" })];
    const { onSelect } = mount({ sessionId: "only" });
    await confirmDelete("Only chat");
    await waitFor(() => expect(m.del).toHaveBeenCalled());
    act(() => (m.del.mock.calls[0][1] as { onSuccess: () => void }).onSuccess());
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("deleting another chat leaves the open one alone", async () => {
    const { onSelect } = mount({ sessionId: "c" });
    await confirmDelete("Today chat");
    await waitFor(() => expect(m.del).toHaveBeenCalled());
    act(() => (m.del.mock.calls[0][1] as { onSuccess: () => void }).onSuccess());
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("deletes a project after confirming", async () => {
    mount();
    openMenu("Project options: Q4 pricing");
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete project" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete project" }));
    await waitFor(() => expect(m.delProject).toHaveBeenCalledWith("p1", expect.anything()));
  });

  it("opens the new-project sheet", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(screen.getByRole("dialog", { name: "New project" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Name" })).toBeInTheDocument();
  });

  it("collapses with aria-expanded on the toggle", () => {
    const onToggle = vi.fn();
    const { rerender } = mount({ onToggle });
    const toggle = screen.getByRole("button", { name: "Hide chats" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle);
    expect(onToggle).toHaveBeenCalled();
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <EditionCompositionProvider extensions={[]}>
          <CopilotSidebar
            agentId="x"
            sessionId={null}
            onSelect={vi.fn()}
            collapsed
            onToggle={onToggle}
          />
        </EditionCompositionProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("button", { name: "Show chats" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("button", { name: "Today chat" })).toBeNull();
  });

  it("shows a skeleton while loading and an empty hint without chats", () => {
    m.loading = true;
    const { unmount } = mount();
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull();
    unmount();
    m.loading = false;
    m.sessions = [];
    m.projects = [];
    mount();
    expect(screen.getByText(/No chats yet/)).toBeInTheDocument();
  });

  it("renders edition sections at the end, one failure not taking the sidebar down", () => {
    const onOpenSession = vi.fn();
    const ext: FrontendEditionExtension = {
      id: "ent",
      capabilities: [],
      chat: {
        chatSidebarSections: [
          {
            id: "broken",
            component: () => {
              throw new Error("boom");
            },
          },
          {
            id: "search",
            component: ({ onOpenSession: open }) => (
              <button type="button" onClick={() => open("z")}>
                Search chats
              </button>
            ),
          },
        ],
      },
    };
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { onSelect } = mount({}, [ext]);
    spy.mockRestore();
    expect(screen.getByRole("button", { name: "Today chat" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Search chats" }));
    expect(onSelect).toHaveBeenCalledWith("z");
    void onOpenSession;
  });

  it("under 640 px the list opens from a Chats button in a sheet", () => {
    m.narrow = true;
    const { onSelect } = mount();
    expect(screen.queryByRole("navigation", { name: "Chats" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Chats" }));
    const sheet = screen.getByRole("dialog");
    expect(within(sheet).getByRole("navigation", { name: "Chats" })).toBeInTheDocument();
    fireEvent.click(within(sheet).getByRole("button", { name: "Today chat" }));
    expect(onSelect).toHaveBeenCalledWith("b");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
