import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const sessionsMock = vi.fn();
const createSessionMock = vi.fn();
const messagesMock = vi.fn();
const sendMessageMock = vi.fn();
const renameSessionMock = vi.fn();
const deleteSessionMock = vi.fn();
const uploadAttachmentMock = vi.fn();
// Defaulted here, not reset in every describe's beforeEach: none of the
// existing suites care about clarifications, and a bare `vi.fn()` default
// (returning undefined) would make `useClarifications`'s `{ data }`
// destructure throw before any of those tests get to render.
const clarificationsMock = vi.fn(() => ({ data: [] }));
const answerClarificationMock = vi.fn();
// Constant defaults for the composer's own data hooks -- these are called on
// every render regardless of which describe block is running, so they need a
// safe default even in suites that never touch `/` or `#`. The one seeded
// knowledge base ("kb-1"/"Preisliste") is reused by every sigil test that
// needs one, rather than re-declared per test.
const chatModesMock = vi.fn(() => ({
  data: [
    {
      key: "ask",
      summary: "Answer from what you already know. No tools, no systems touched.",
      allowsTools: false,
      allowsWrites: false,
    },
    {
      key: "plan",
      summary: "Work out the steps and show them. Changes nothing.",
      allowsTools: true,
      allowsWrites: false,
    },
    {
      key: "do",
      summary: "Carry out what was agreed, under the usual guardrails.",
      allowsTools: true,
      allowsWrites: true,
    },
    {
      key: "summarise",
      summary: "Condense this conversation into a decision record.",
      allowsTools: false,
      allowsWrites: false,
    },
  ],
}));
const knowledgeBasesMock = vi.fn(() => ({
  data: {
    items: [{ id: "kb-1", name: "Preisliste", description: "Preisliste 2026" }],
    totalCount: 1,
  },
  isLoading: false,
}));

vi.mock("@/lib/hooks-chat", () => ({
  useChatSessions: (agentId?: string) => sessionsMock(agentId),
  useCreateChatSession: () => ({ mutate: createSessionMock, isPending: false }),
  useChatMessages: () => messagesMock(),
  useSendChatMessage: () => ({ mutate: sendMessageMock, isPending: false }),
  useRenameChatSession: () => ({ mutate: renameSessionMock }),
  useDeleteChatSession: () => ({ mutate: deleteSessionMock }),
  useUploadChatAttachment: () => ({ mutate: uploadAttachmentMock, isPending: false }),
}));

vi.mock("@/lib/hooks", () => ({
  useClarifications: () => clarificationsMock(),
  useAnswerClarification: () => ({ mutate: answerClarificationMock, isPending: false }),
  useKnowledgeBases: () => knowledgeBasesMock(),
}));

// The pure helpers stay REAL -- they are the thing under test here; only the
// network hook is replaced.
vi.mock("@/lib/chat-commands", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/chat-commands")>();
  return { ...actual, useChatModes: () => chatModesMock() };
});
vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useCan: () => () => true };
});

import { ChatWindow } from "@/components/chat-window";

function renderChat() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ChatWindow agentId="agent-1" agentName="Nora" />
    </QueryClientProvider>,
  );
}

describe("ChatWindow", () => {
  beforeEach(() => {
    sessionsMock.mockReset();
    createSessionMock.mockReset();
    messagesMock.mockReset();
    sendMessageMock.mockReset();
    renameSessionMock.mockReset();
    deleteSessionMock.mockReset();
    uploadAttachmentMock.mockReset();
  });

  it("offers to start a chat when the agent has no sessions yet", () => {
    sessionsMock.mockReturnValue({ data: [], isLoading: false });
    messagesMock.mockReturnValue({ data: undefined, isLoading: false });
    renderChat();
    expect(screen.getByRole("button", { name: /start chat/i })).toBeInTheDocument();
  });

  it("starting a chat creates a new session for this agent", () => {
    sessionsMock.mockReturnValue({ data: [], isLoading: false });
    messagesMock.mockReturnValue({ data: undefined, isLoading: false });
    renderChat();
    fireEvent.click(screen.getByRole("button", { name: /start chat/i }));
    expect(createSessionMock).toHaveBeenCalledWith("agent-1", expect.anything());
  });

  it("renders the transcript once a session and its messages exist", () => {
    sessionsMock.mockReturnValue({
      data: [{ id: "s1", agentId: "agent-1", title: "", createdAt: "2026-08-27T00:00:00Z" }],
      isLoading: false,
    });
    messagesMock.mockReturnValue({
      data: [
        {
          id: "m1",
          sessionId: "s1",
          role: "user",
          content: "Hallo",
          runId: null,
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:00Z",
          mode: null,
          contextRefs: [],
        },
        {
          id: "m2",
          sessionId: "s1",
          role: "assistant",
          content: "Hi, wie kann ich helfen?",
          runId: "r1",
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:01Z",
          mode: null,
          contextRefs: [],
        },
      ],
      isLoading: false,
    });
    renderChat();
    expect(screen.getByText("Hallo")).toBeInTheDocument();
    expect(screen.getByText("Hi, wie kann ich helfen?")).toBeInTheDocument();
  });

  it("shows a thinking indicator and disables sending while the last turn is the user's own", () => {
    sessionsMock.mockReturnValue({
      data: [{ id: "s1", agentId: "agent-1", title: "", createdAt: "2026-08-27T00:00:00Z" }],
      isLoading: false,
    });
    messagesMock.mockReturnValue({
      data: [
        {
          id: "m1",
          sessionId: "s1",
          role: "user",
          content: "Wie viele Stunden diese Woche?",
          runId: null,
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:00Z",
          mode: null,
          contextRefs: [],
        },
      ],
      isLoading: false,
    });
    renderChat();
    expect(screen.getByText(/is thinking/i)).toBeInTheDocument();
  });

  it("renders a rendered component attached to an assistant reply", () => {
    sessionsMock.mockReturnValue({
      data: [{ id: "s1", agentId: "agent-1", title: "", createdAt: "2026-08-27T00:00:00Z" }],
      isLoading: false,
    });
    messagesMock.mockReturnValue({
      data: [
        {
          id: "m2",
          sessionId: "s1",
          role: "assistant",
          content: "Hier ist dein Bericht.",
          runId: "r1",
          renderedComponents: [
            {
              componentKey: "record_card",
              props: { title: "Wochenbericht", fields: [{ label: "Stunden", value: "38.5" }] },
            },
          ],
          createdAt: "2026-08-27T00:00:01Z",
          mode: null,
          contextRefs: [],
        },
      ],
      isLoading: false,
    });
    renderChat();
    expect(screen.getByText("Wochenbericht")).toBeInTheDocument();
  });
});

describe("ChatWindow waiting_for_input", () => {
  beforeEach(() => {
    sessionsMock.mockReset();
    messagesMock.mockReset();
    answerClarificationMock.mockReset();
    clarificationsMock.mockReturnValue({ data: [] });
    sessionsMock.mockReturnValue({
      data: [{ id: "s1", agentId: "agent-1", title: "", createdAt: "2026-08-27T00:00:00Z" }],
      isLoading: false,
    });
    messagesMock.mockReturnValue({
      data: [
        {
          id: "m1",
          sessionId: "s1",
          role: "user",
          content: "Wo soll ich den Export ablegen?",
          runId: null,
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:00Z",
          mode: null,
          contextRefs: [],
        },
      ],
      isLoading: false,
    });
  });

  // A run parked here never gets an assistant ChatMessage (executor.py
  // skips record_assistant_reply for waiting_for_input on purpose), so
  // without reading the open Clarification the box would say "is
  // thinking…" forever with no way for the reader to unblock it.
  it("shows the agent's question instead of the thinking indicator once a clarification is open", () => {
    clarificationsMock.mockReturnValue({
      data: [
        {
          id: "c1",
          runId: "r1",
          agentId: "agent-1",
          agentName: "Nora",
          departmentId: "d1",
          departmentName: "Ops",
          question: "Odoo Documents, Odoo Spreadsheets, or a manual export?",
          status: "open",
          createdAt: "2026-08-27T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    renderChat();
    expect(
      screen.getByText("Odoo Documents, Odoo Spreadsheets, or a manual export?"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/is thinking/i)).not.toBeInTheDocument();
  });

  it("submits the typed reply as the clarification's answer, not a new chat message", () => {
    clarificationsMock.mockReturnValue({
      data: [
        {
          id: "c1",
          runId: "r1",
          agentId: "agent-1",
          agentName: "Nora",
          departmentId: "d1",
          departmentName: "Ops",
          question: "Odoo Documents, Odoo Spreadsheets, or a manual export?",
          status: "open",
          createdAt: "2026-08-27T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    renderChat();

    fireEvent.change(screen.getByPlaceholderText(/your answer/i), {
      target: { value: "Odoo Documents" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^answer$/i }));

    expect(answerClarificationMock).toHaveBeenCalledWith(
      { clarificationId: "c1", answer: "Odoo Documents" },
      expect.anything(),
    );
    expect(sendMessageMock).not.toHaveBeenCalled();
  });

  it("falls back to the thinking indicator when no clarification is open for this agent", () => {
    clarificationsMock.mockReturnValueOnce({ data: [], isLoading: false });
    renderChat();
    expect(screen.getByText(/is thinking/i)).toBeInTheDocument();
  });
});

describe("ChatWindow session picker", () => {
  beforeEach(() => {
    sessionsMock.mockReset();
    createSessionMock.mockReset();
    messagesMock.mockReset();
    sendMessageMock.mockReset();
    renameSessionMock.mockReset();
    deleteSessionMock.mockReset();
    sessionsMock.mockReturnValue({
      data: [
        { id: "s1", agentId: "agent-1", title: "VPN Tickets", createdAt: "2026-08-27T00:00:00Z" },
        { id: "s2", agentId: "agent-1", title: "", createdAt: "2026-08-26T00:00:00Z" },
      ],
      isLoading: false,
    });
    messagesMock.mockReturnValue({ data: [], isLoading: false });
  });

  // Radix's DropdownMenuTrigger opens on pointerdown, not click -- jsdom's
  // fireEvent.click doesn't synthesize a pointerdown the way a real browser
  // click does, so a plain click silently never opens the menu.
  function openPicker() {
    fireEvent.pointerDown(screen.getByRole("button", { name: "VPN Tickets" }), { button: 0 });
  }

  it("shows the current session's title on the picker trigger, defaulting to the newest session", () => {
    renderChat();
    expect(screen.getByRole("button", { name: "VPN Tickets" })).toBeInTheDocument();
  });

  it("lists every session in the dropdown, falling back to a placeholder for an unnamed one", () => {
    renderChat();
    openPicker();
    expect(screen.getAllByText("VPN Tickets").length).toBeGreaterThan(0);
    expect(screen.getByText("Untitled chat")).toBeInTheDocument();
  });

  it("renaming a session commits the new title on Enter", async () => {
    renderChat();
    openPicker();
    fireEvent.click(screen.getAllByRole("button", { name: /rename/i })[0]);

    const input = screen.getByDisplayValue("VPN Tickets");
    fireEvent.change(input, { target: { value: "Zugriff Laufwerk" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() =>
      expect(renameSessionMock).toHaveBeenCalledWith({
        sessionId: "s1",
        title: "Zugriff Laufwerk",
      }),
    );
  });

  it("blank input on blur does not persist a rename", () => {
    renderChat();
    openPicker();
    fireEvent.click(screen.getAllByRole("button", { name: /rename/i })[0]);

    const input = screen.getByDisplayValue("VPN Tickets");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.blur(input);

    expect(renameSessionMock).not.toHaveBeenCalled();
  });

  it("deleting a session asks for confirmation before persisting", async () => {
    renderChat();
    openPicker();
    fireEvent.click(screen.getAllByRole("button", { name: /delete/i })[0]);

    expect(deleteSessionMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));

    await waitFor(() => expect(deleteSessionMock).toHaveBeenCalledWith("s1", expect.anything()));
  });

  it("cancelling the delete confirmation leaves the session untouched", () => {
    renderChat();
    openPicker();
    fireEvent.click(screen.getAllByRole("button", { name: /delete/i })[0]);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    expect(deleteSessionMock).not.toHaveBeenCalled();
  });
});

describe("ChatWindow attachments", () => {
  beforeEach(() => {
    sessionsMock.mockReset();
    createSessionMock.mockReset();
    messagesMock.mockReset();
    sendMessageMock.mockReset();
    renameSessionMock.mockReset();
    deleteSessionMock.mockReset();
    uploadAttachmentMock.mockReset();
    sessionsMock.mockReturnValue({
      data: [{ id: "s1", agentId: "agent-1", title: "", createdAt: "2026-08-27T00:00:00Z" }],
      isLoading: false,
    });
  });

  const attachmentDto = {
    id: "att1",
    filename: "notes.txt",
    contentType: "text/plain",
    sizeBytes: 12,
    isImage: false,
    createdAt: "2026-08-27T00:00:00Z",
  };

  it("uploading a file adds a pending attachment chip", () => {
    messagesMock.mockReturnValue({ data: [], isLoading: false });
    uploadAttachmentMock.mockImplementation(
      (_file: File, opts?: { onSuccess?: (dto: unknown) => void }) => {
        opts?.onSuccess?.(attachmentDto);
      },
    );
    renderChat();

    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText(/attach a file/i), { target: { files: [file] } });

    expect(uploadAttachmentMock).toHaveBeenCalledWith(file, expect.anything());
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
  });

  it("refuses a file over the 25 MB cap without calling the upload", () => {
    // Client half of the cap: fast feedback only -- the server's own check is
    // authoritative. Without this the browser uploaded the whole thing just to
    // be told 413.
    messagesMock.mockReturnValue({ data: [], isLoading: false });
    renderChat();

    const huge = new File(["x"], "huge.pdf", { type: "application/pdf" });
    Object.defineProperty(huge, "size", { value: 26 * 1024 * 1024 });
    fireEvent.change(screen.getByLabelText(/attach a file/i), { target: { files: [huge] } });

    expect(uploadAttachmentMock).not.toHaveBeenCalled();
    expect(screen.queryByText("huge.pdf")).not.toBeInTheDocument();
  });

  it("removing a pending chip before send removes it", () => {
    messagesMock.mockReturnValue({ data: [], isLoading: false });
    uploadAttachmentMock.mockImplementation(
      (_file: File, opts?: { onSuccess?: (dto: unknown) => void }) => {
        opts?.onSuccess?.(attachmentDto);
      },
    );
    renderChat();

    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText(/attach a file/i), { target: { files: [file] } });
    expect(screen.getByText("notes.txt")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /remove/i }));
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument();
  });

  it("sending carries the pending attachment ids and clears them on success", () => {
    messagesMock.mockReturnValue({ data: [], isLoading: false });
    uploadAttachmentMock.mockImplementation(
      (_file: File, opts?: { onSuccess?: (dto: unknown) => void }) => {
        opts?.onSuccess?.(attachmentDto);
      },
    );
    renderChat();

    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText(/attach a file/i), { target: { files: [file] } });

    const textarea = screen.getByPlaceholderText(/type a message/i);
    fireEvent.change(textarea, { target: { value: "see attached" } });
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));

    expect(sendMessageMock).toHaveBeenCalledWith(
      { message: "see attached", attachmentIds: ["att1"], contextRefs: [] },
      expect.anything(),
    );
  });

  it("a sent message with an attachment renders its chip", () => {
    messagesMock.mockReturnValue({
      data: [
        {
          id: "m1",
          sessionId: "s1",
          role: "user",
          content: "see attached",
          runId: null,
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:00Z",
          attachments: [attachmentDto],
          mode: null,
          contextRefs: [],
        },
      ],
      isLoading: false,
    });
    renderChat();

    expect(screen.getByText("see attached")).toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
  });
});

describe("ChatWindow agent switching", () => {
  beforeEach(() => {
    sessionsMock.mockReset();
    createSessionMock.mockReset();
    messagesMock.mockReset();
    sendMessageMock.mockReset();
    renameSessionMock.mockReset();
    deleteSessionMock.mockReset();
  });

  // Both real call sites (chat.tsx, agents.$id.tsx) key ChatWindow by
  // agentId specifically so switching agents remounts it -- without that
  // key, the previous agent's sessionId lives on as local state and the
  // reader ends up chatting into a DIFFERENT agent's session while the
  // header shows the newly selected agent's name.
  it("keying by agentId resets to the new agent's own session, not the previous agent's", () => {
    sessionsMock.mockImplementation((agentId?: string) =>
      agentId === "agent-1"
        ? {
            data: [
              {
                id: "s1",
                agentId: "agent-1",
                title: "Agent 1 chat",
                createdAt: "2026-08-27T00:00:00Z",
              },
            ],
            isLoading: false,
          }
        : { data: [], isLoading: false },
    );
    messagesMock.mockReturnValue({ data: [], isLoading: false });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <ChatWindow key="agent-1" agentId="agent-1" agentName="Nora" />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("button", { name: "Agent 1 chat" })).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={qc}>
        <ChatWindow key="agent-2" agentId="agent-2" agentName="Tim" />
      </QueryClientProvider>,
    );

    // Agent 2 has no sessions of its own -- if state hadn't reset, Agent 1's
    // session picker/messages would still be showing here.
    expect(screen.queryByRole("button", { name: "Agent 1 chat" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start chat/i })).toBeInTheDocument();
  });
});

describe("ChatWindow composer sigils", () => {
  beforeEach(() => {
    sessionsMock.mockReset();
    createSessionMock.mockReset();
    messagesMock.mockReset();
    sendMessageMock.mockReset();
    renameSessionMock.mockReset();
    deleteSessionMock.mockReset();
    uploadAttachmentMock.mockReset();
    sessionsMock.mockReturnValue({
      data: [{ id: "s1", agentId: "agent-1", title: "", createdAt: "2026-08-27T00:00:00Z" }],
      isLoading: false,
    });
    messagesMock.mockReturnValue({ data: [], isLoading: false });
  });

  function typeDraft(text: string) {
    const textarea = screen.getByPlaceholderText(/type a message/i) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: text, selectionStart: text.length } });
    return textarea;
  }

  it("typing '/' opens the command picker with a description per command", () => {
    renderChat();
    typeDraft("/");
    expect(screen.getByRole("listbox", { name: /commands/i })).toBeInTheDocument();
    expect(
      screen.getByText("Answer from what you already know. No tools, no systems touched."),
    ).toBeInTheDocument();
  });

  it("picking a command puts it in the draft and shows the armed pill", () => {
    renderChat();
    const textarea = typeDraft("/");
    fireEvent.click(screen.getByRole("option", { name: /\/plan/i }));
    expect(textarea.value).toBe("/plan ");
    // The pill and the (re-offered) picker option both render the literal
    // "/plan" text; either is proof the pick landed in the draft.
    expect(screen.getAllByText("/plan").length).toBeGreaterThan(0);
  });

  it("an unknown slash command is still sendable", () => {
    renderChat();
    const textarea = typeDraft("/deploy the thing");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(sendMessageMock).toHaveBeenCalledWith(
      { message: "/deploy the thing", attachmentIds: [], contextRefs: [] },
      expect.anything(),
    );
  });

  it("a path is not a command", () => {
    renderChat();
    typeDraft("/etc/passwd is world readable?");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    // The armed pill is a <span>; excluding it as a selector keeps this from
    // matching the textarea's own value, which also starts with "/etc".
    expect(screen.queryByText(/^\/etc/, { selector: "span" })).not.toBeInTheDocument();
  });

  it("typing '#' offers knowledge bases and picking one adds a chip", () => {
    renderChat();
    typeDraft("#");
    expect(screen.getByRole("listbox", { name: /attach context/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /Preisliste/i }));

    expect(screen.getByText("Preisliste")).toBeInTheDocument();
    const textarea = screen.getByPlaceholderText(/type a message/i) as HTMLTextAreaElement;
    expect(textarea.value).not.toContain("#");
  });

  it("sending carries the context ref and then clears it", () => {
    renderChat();
    typeDraft("#");
    fireEvent.click(screen.getByRole("option", { name: /Preisliste/i }));
    expect(screen.getByText("Preisliste")).toBeInTheDocument();

    const textarea = screen.getByPlaceholderText(/type a message/i);
    fireEvent.change(textarea, {
      target: { value: "what's in here?", selectionStart: 16 },
    });
    fireEvent.click(screen.getByRole("button", { name: /^send$/i }));

    expect(sendMessageMock).toHaveBeenCalledWith(
      {
        message: "what's in here?",
        attachmentIds: [],
        contextRefs: [{ kind: "knowledge_base", id: "kb-1" }],
      },
      expect.anything(),
    );

    const [, opts] = sendMessageMock.mock.calls[0] as [
      unknown,
      { onSuccess?: () => void } | undefined,
    ];
    act(() => {
      opts?.onSuccess?.();
    });
    expect(screen.queryByText("Preisliste")).not.toBeInTheDocument();
  });

  it("Enter picks from the picker instead of sending", () => {
    renderChat();
    const textarea = typeDraft("/");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(sendMessageMock).not.toHaveBeenCalled();
  });

  it("typing '@' does not open a picker in the agent-scoped chat", () => {
    // Decision, not omission: switching agent is the front door's job -- see
    // this task's scope note. Typing `@` here just types an `@`.
    renderChat();
    typeDraft("ask @sa");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("the composer hint names all three sigils", () => {
    renderChat();
    expect(screen.getByTestId("composer-hint")).toBeInTheDocument();
  });

  it("a sent turn shows its mode badge and its context chip", () => {
    messagesMock.mockReturnValue({
      data: [
        {
          id: "m1",
          sessionId: "s1",
          role: "user",
          content: "check the price list",
          runId: null,
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:00Z",
          mode: "plan",
          contextRefs: [{ kind: "knowledge_base", id: "kb-1", label: "Preisliste" }],
        },
      ],
      isLoading: false,
    });
    renderChat();
    expect(screen.getByText("/plan")).toBeInTheDocument();
    expect(screen.getByText("Preisliste")).toBeInTheDocument();
  });

  it("the paperclip and the # picker never offer the same thing", () => {
    renderChat();
    expect(screen.getByLabelText(/attach a file/i)).toBeInTheDocument();

    typeDraft("#");
    const listbox = screen.getByRole("listbox", { name: /attach context/i });
    expect(listbox).toBeInTheDocument();
    expect(within(listbox).queryByText(/attach a file/i)).not.toBeInTheDocument();
  });
});
