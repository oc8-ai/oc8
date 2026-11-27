import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { AgentInstructionsPanel } from "@/components/agent-instructions-panel";

const {
  getHistory,
  patchInstructions,
  assistantMock,
  sessionsMock,
  createSessionMock,
  messagesMock,
  sendMessageMock,
  listInstructionFilesMock,
  uploadInstructionFileMock,
  deleteFileMock,
  deleteMock,
  toastError,
} = vi.hoisted(() => ({
  getHistory: vi.fn((_url: string) => ({ revisions: [], totalCount: 0, nextBeforeSeq: null })),
  patchInstructions: vi.fn(),
  assistantMock: vi.fn(),
  sessionsMock: vi.fn(),
  createSessionMock: vi.fn(),
  messagesMock: vi.fn(),
  sendMessageMock: vi.fn(),
  listInstructionFilesMock: vi.fn(() => []),
  uploadInstructionFileMock: vi.fn(),
  deleteFileMock: vi.fn(),
  deleteMock: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("sonner", async (orig) => {
  const actual = await orig<typeof import("sonner")>();
  return { ...actual, toast: Object.assign(actual.toast, { error: toastError }) };
});

vi.mock("@/lib/api", () => ({
  api: {
    get: (url: string) => {
      if (url.includes("/instructions/history")) return getHistory(url);
      throw new Error(`unexpected GET ${url}`);
    },
    patch: (url: string, body: unknown) => {
      if (url.endsWith("/instructions")) return patchInstructions(body);
      throw new Error(`unexpected PATCH ${url}`);
    },
    delete: (url: string) => deleteMock(url),
  },
  listInstructionFiles: (agentId: string) => listInstructionFilesMock(agentId),
  uploadInstructionFile: (agentId: string, file: File) => uploadInstructionFileMock(agentId, file),
  deleteFile: (fileId: string) => deleteFileMock(fileId),
  // Real value, not a stand-in: the panel's client-side size check reads it,
  // and leaving it undefined would silently disable that check here.
  MAX_ATTACHMENT_BYTES: 25 * 1024 * 1024,
}));

// Only useAssistant is mocked here -- useAgentInstructionHistory,
// useUpdateAgentInstructions, and the three "Attached files" hooks
// (useAgentInstructionFiles/useUploadInstructionFile/useDeleteInstructionFile)
// keep running for real, against the @/lib/api mock above, exactly as the
// pre-existing tests already relied on.
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return { ...actual, useAssistant: () => assistantMock() };
});

vi.mock("@/lib/hooks-chat", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks-chat")>()),
  useChatSessions: () => sessionsMock(),
  useCreateChatSession: () => ({ mutate: createSessionMock, isPending: false }),
  useChatMessages: () => messagesMock(),
  useSendChatMessage: () => ({ mutate: sendMessageMock, isPending: false }),
}));

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
  return { ...result, qc };
}

describe("AgentInstructionsPanel", () => {
  beforeEach(() => {
    getHistory.mockReset();
    getHistory.mockReturnValue({ revisions: [] });
    patchInstructions.mockReset();
    assistantMock.mockReset();
    assistantMock.mockReturnValue({ data: undefined });
    sessionsMock.mockReset();
    sessionsMock.mockReturnValue({ data: undefined });
    createSessionMock.mockReset();
    messagesMock.mockReset();
    messagesMock.mockReturnValue({ data: undefined });
    sendMessageMock.mockReset();
    listInstructionFilesMock.mockReset();
    listInstructionFilesMock.mockReturnValue([]);
    uploadInstructionFileMock.mockReset();
    deleteFileMock.mockReset();
    deleteMock.mockReset();
    deleteMock.mockResolvedValue(undefined);
    toastError.mockReset();
  });

  it("saves an edited instructions text and disables Save until something changes", async () => {
    patchInstructions.mockResolvedValue({ id: "agent-1", mission: "Work IT helpdesk tickets" });

    renderWithClient(
      <AgentInstructionsPanel agentId="agent-1" mission="Old mission" mayManage={true} />,
    );

    const saveButton = screen.getByRole("button", { name: /save/i });
    expect(saveButton).toBeDisabled();

    const textarea = screen.getByPlaceholderText(/sent with every run/i);
    fireEvent.change(textarea, { target: { value: "Work IT helpdesk tickets" } });
    expect(saveButton).not.toBeDisabled();

    fireEvent.click(saveButton);

    await waitFor(() =>
      expect(patchInstructions).toHaveBeenCalledWith({
        instructions: "Work IT helpdesk tickets",
      }),
    );
  });

  it("is read-only when the caller lacks agent:manage, with no Save or Restore controls", async () => {
    getHistory.mockReturnValue({
      revisions: [
        { ts: "2026-08-19T10:00:00Z", before: "legacy text", after: "Old mission", by: "justin" },
      ],
    });

    renderWithClient(
      <AgentInstructionsPanel agentId="agent-1" mission="Old mission" mayManage={false} />,
    );

    expect(screen.getByPlaceholderText(/sent with every run/i)).toBeDisabled();
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
    await screen.findByText("legacy text");
    expect(screen.queryByRole("button", { name: /restore/i })).not.toBeInTheDocument();
  });

  it("numbers versions newest-first (current = highest, Default) down to v1", async () => {
    getHistory.mockReturnValue({
      revisions: [
        { ts: "2026-08-20T10:00:00Z", before: "beta text", after: "current text", by: "justin" },
        { ts: "2026-08-19T10:00:00Z", before: "alpha text", after: "beta text", by: "justin" },
      ],
    });

    renderWithClient(
      <AgentInstructionsPanel agentId="agent-1" mission="current text" mayManage={true} />,
    );

    await screen.findByText("alpha text");
    expect(screen.getByText("beta text")).toBeInTheDocument();
    // "current text" also appears in the editor's own textarea value, so
    // scope this assertion to the version list's preview paragraphs only.
    const previews = screen.getAllByText(
      (_, el) => el?.tagName === "P" && el.className.includes("line-clamp-2"),
    );
    expect(previews.map((p) => p.textContent)).toContain("current text");
    expect(screen.getAllByText("v1")).toHaveLength(1);
    expect(screen.getAllByText("v2")).toHaveLength(1);
    expect(screen.getAllByText("v3")).toHaveLength(1);
    expect(screen.getByText(/default/i)).toBeInTheDocument();
    expect(screen.getByText(/since creation/i)).toBeInTheDocument();
  });

  it("restoring a version loads its text into the editor without saving immediately", async () => {
    getHistory.mockReturnValue({
      revisions: [
        { ts: "2026-08-20T10:00:00Z", before: "beta text", after: "current text", by: "justin" },
        { ts: "2026-08-19T10:00:00Z", before: "alpha text", after: "beta text", by: "justin" },
      ],
    });

    renderWithClient(
      <AgentInstructionsPanel agentId="agent-1" mission="current text" mayManage={true} />,
    );

    await screen.findByText("beta text");
    const restoreButtons = screen.getAllByRole("button", { name: /restore/i });
    expect(restoreButtons).toHaveLength(2); // beta text (v2) and alpha text (v1), not current (v3)
    fireEvent.click(restoreButtons[0]);

    const textarea = screen.getByPlaceholderText(/sent with every run/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("beta text");
    expect(screen.getByRole("button", { name: /^save$/i })).not.toBeDisabled();
    expect(patchInstructions).not.toHaveBeenCalled();
  });

  it("lets you jump back to the still-current (Default) version after loading an older one", async () => {
    // Regression: the Default row's Restore button was gated on `!isCurrent`,
    // so once an older version was loaded into the editor there was no way
    // back to the actual current version without retyping it by hand.
    getHistory.mockReturnValue({
      revisions: [
        { ts: "2026-08-20T10:00:00Z", before: "beta text", after: "current text", by: "justin" },
        { ts: "2026-08-19T10:00:00Z", before: "alpha text", after: "beta text", by: "justin" },
      ],
    });

    renderWithClient(
      <AgentInstructionsPanel agentId="agent-1" mission="current text" mayManage={true} />,
    );

    await screen.findByText("alpha text");
    // Jump to the oldest version first, like the reported bug: v1, then try
    // to get back to v2 (the still-current/Default one).
    fireEvent.click(screen.getAllByRole("button", { name: /restore/i })[1]);

    const textarea = screen.getByPlaceholderText(/sent with every run/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe("alpha text");

    // Two rows now offer Restore (beta text and the still-current default) --
    // pick the one sitting in the Default row specifically.
    const defaultBadge = screen.getByText(/default/i);
    const defaultRow = defaultBadge.closest("li")!;
    const backToCurrent = within(defaultRow).getByRole("button", { name: /restore/i });
    fireEvent.click(backToCurrent);
    expect(textarea.value).toBe("current text");
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
    expect(patchInstructions).not.toHaveBeenCalled();
  });

  it("shows a single Default v1 entry with no Restore button when the agent has never been edited", async () => {
    renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={true} />);
    expect(await screen.findByText("v1")).toBeInTheDocument();
    expect(screen.getByText(/default/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /restore/i })).not.toBeInTheDocument();
  });

  it("loads history a page at a time and keeps version numbers correct across pages", async () => {
    // Guards the perf concern that prompted pagination: a hundred edits
    // must not all load at once. Two revisions total, split across two
    // pages of one each, with the true total (2) reported from page one --
    // v-numbering must stay 3/2/1 even before the older page is fetched.
    getHistory.mockImplementation((url: string) =>
      url.includes("beforeSeq")
        ? {
            revisions: [
              { ts: "2026-08-19T10:00:00Z", before: "old text", after: "mid text", by: "justin" },
            ],
            totalCount: 2,
            nextBeforeSeq: null,
          }
        : {
            revisions: [
              {
                ts: "2026-08-20T10:00:00Z",
                before: "mid text",
                after: "current text",
                by: "justin",
              },
            ],
            totalCount: 2,
            nextBeforeSeq: 555,
          },
    );

    renderWithClient(
      <AgentInstructionsPanel agentId="agent-1" mission="current text" mayManage={true} />,
    );

    await screen.findByText("mid text");
    expect(screen.getByText("v3")).toBeInTheDocument(); // current, Default
    expect(screen.getByText("v2")).toBeInTheDocument(); // mid text
    expect(screen.queryByText("v1")).not.toBeInTheDocument(); // not loaded yet
    expect(screen.queryByText(/since creation/i)).not.toBeInTheDocument();

    const loadMore = screen.getByRole("button", { name: /load older versions/i });
    fireEvent.click(loadMore);

    await screen.findByText("old text");
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText(/since creation/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /load older versions/i })).not.toBeInTheDocument();
  });

  describe("Draft with copilot", () => {
    it("with no existing Assistant session, Generate creates one and then sends the drafting request", () => {
      assistantMock.mockReturnValue({ data: { agentId: "assistant-1" } });
      sessionsMock.mockReturnValue({ data: [] });
      createSessionMock.mockImplementation(
        (agentId: string, opts?: { onSuccess?: (s: unknown) => void }) => {
          opts?.onSuccess?.({
            id: "new-session",
            agentId,
            title: "",
            createdAt: "2026-08-27T00:00:00Z",
            lastMessageAt: null,
          });
        },
      );

      renderWithClient(
        <AgentInstructionsPanel
          agentId="agent-1"
          agentName="Nora"
          mission="Old mission"
          mayManage={true}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /draft with copilot/i }));
      fireEvent.change(screen.getByPlaceholderText(/handles first-level it support/i), {
        target: { value: "Answer billing questions" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^generate$/i }));

      expect(createSessionMock).toHaveBeenCalledWith("assistant-1", expect.anything());
      expect(sendMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          message: expect.stringContaining("Answer billing questions"),
        }),
        expect.anything(),
      );
    });

    it("shows the assistant's reply as a draft preview and inserts it into the editor", () => {
      assistantMock.mockReturnValue({ data: { agentId: "assistant-1" } });
      sessionsMock.mockReturnValue({
        data: [
          {
            id: "s1",
            agentId: "assistant-1",
            title: "",
            createdAt: "2026-08-27T00:00:00Z",
            lastMessageAt: null,
          },
        ],
      });
      sendMessageMock.mockImplementation(
        (body: { message: string }, opts?: { onSuccess?: (m: unknown) => void }) => {
          opts?.onSuccess?.({
            id: "u1",
            sessionId: "s1",
            role: "user",
            content: body.message,
            runId: "run-draft",
            renderedComponents: [],
            createdAt: "2026-08-27T00:00:01Z",
          });
        },
      );
      let liveMessages: unknown[] = [];
      messagesMock.mockImplementation(() => ({ data: liveMessages }));

      const { rerender, qc } = renderWithClient(
        <AgentInstructionsPanel
          agentId="agent-1"
          agentName="Nora"
          mission="Old mission"
          mayManage={true}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /draft with copilot/i }));
      fireEvent.change(screen.getByPlaceholderText(/handles first-level it support/i), {
        target: { value: "Answer billing questions" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^generate$/i }));

      // No session had to be created (one already existed) -- send goes
      // straight out, no createSession round trip.
      expect(createSessionMock).not.toHaveBeenCalled();
      expect(sendMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          message: expect.stringContaining("Answer billing questions"),
        }),
        expect.anything(),
      );

      // The transcript now "polls in" the user turn plus the assistant's
      // reply -- re-rendering with the same client is what a real poll tick
      // would also do (a fresh useChatMessages return value on the next
      // render), since the hook itself is mocked out here.
      liveMessages = [
        {
          id: "u1",
          sessionId: "s1",
          role: "user",
          content: "generate...",
          runId: null,
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:01Z",
        },
        {
          id: "a1",
          sessionId: "s1",
          role: "assistant",
          content: "Handles billing questions end to end.",
          runId: "run-draft",
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:02Z",
        },
      ];
      rerender(
        <QueryClientProvider client={qc}>
          <AgentInstructionsPanel
            agentId="agent-1"
            agentName="Nora"
            mission="Old mission"
            mayManage={true}
          />
        </QueryClientProvider>,
      );

      expect(screen.getByText("Handles billing questions end to end.")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /insert into editor/i }));

      const missionTextarea = screen.getByPlaceholderText(
        /sent with every run/i,
      ) as HTMLTextAreaElement;
      expect(missionTextarea.value).toBe("Handles billing questions end to end.");
      // The drawer closes and the rough description is cleared on insert.
      expect(
        screen.queryByPlaceholderText(/handles first-level it support/i),
      ).not.toBeInTheDocument();
    });

    it("an interleaved reply from a different run (e.g. sent through the floating dock) is not mistaken for the draft", () => {
      // Regression: the draft used to be matched by transcript length, so an
      // unrelated message sent through the OTHER surface sharing this same
      // Assistant session (the floating dock) could land in "our" slot and
      // silently become the proposed draft. Matching by run id must not be
      // fooled by that interleaving.
      assistantMock.mockReturnValue({ data: { agentId: "assistant-1" } });
      sessionsMock.mockReturnValue({
        data: [
          {
            id: "s1",
            agentId: "assistant-1",
            title: "",
            createdAt: "2026-08-27T00:00:00Z",
            lastMessageAt: null,
          },
        ],
      });
      sendMessageMock.mockImplementation(
        (body: { message: string }, opts?: { onSuccess?: (m: unknown) => void }) => {
          opts?.onSuccess?.({
            id: "u-draft",
            sessionId: "s1",
            role: "user",
            content: body.message,
            runId: "run-draft",
            renderedComponents: [],
            createdAt: "2026-08-27T00:00:01Z",
          });
        },
      );
      let liveMessages: unknown[] = [];
      messagesMock.mockImplementation(() => ({ data: liveMessages }));

      const { rerender, qc } = renderWithClient(
        <AgentInstructionsPanel
          agentId="agent-1"
          agentName="Nora"
          mission="Old mission"
          mayManage={true}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /draft with copilot/i }));
      fireEvent.change(screen.getByPlaceholderText(/handles first-level it support/i), {
        target: { value: "Answer billing questions" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^generate$/i }));

      // An unrelated exchange -- e.g. someone using the floating dock at the
      // same time -- lands in the shared transcript first, carrying a
      // DIFFERENT run id than the one this Generate request is waiting on.
      liveMessages = [
        {
          id: "u-other",
          sessionId: "s1",
          role: "user",
          content: "What needs approval?",
          runId: null,
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:02Z",
        },
        {
          id: "a-other",
          sessionId: "s1",
          role: "assistant",
          content: "Two proposals are waiting.",
          runId: "run-other",
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:03Z",
        },
        {
          id: "u-draft",
          sessionId: "s1",
          role: "user",
          content: "generate...",
          runId: null,
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:04Z",
        },
      ];
      rerender(
        <QueryClientProvider client={qc}>
          <AgentInstructionsPanel
            agentId="agent-1"
            agentName="Nora"
            mission="Old mission"
            mayManage={true}
          />
        </QueryClientProvider>,
      );

      // The unrelated reply must never become the proposed draft, and
      // Generate must still read as in-flight.
      expect(screen.queryByText("Two proposals are waiting.")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /generating/i })).toBeInTheDocument();

      // The real reply lands, carrying the matching run id.
      liveMessages = [
        ...liveMessages,
        {
          id: "a-draft",
          sessionId: "s1",
          role: "assistant",
          content: "Handles billing questions end to end.",
          runId: "run-draft",
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:05Z",
        },
      ];
      rerender(
        <QueryClientProvider client={qc}>
          <AgentInstructionsPanel
            agentId="agent-1"
            agentName="Nora"
            mission="Old mission"
            mayManage={true}
          />
        </QueryClientProvider>,
      );

      expect(screen.getByText("Handles billing questions end to end.")).toBeInTheDocument();
      expect(screen.queryByText("Two proposals are waiting.")).not.toBeInTheDocument();
    });

    it("while the copilot still answers (202 queued), withdraws the request instead of waiting on that answer", async () => {
      assistantMock.mockReturnValue({ data: { agentId: "assistant-1" } });
      sessionsMock.mockReturnValue({
        data: [
          {
            id: "s1",
            agentId: "assistant-1",
            title: "",
            createdAt: "2026-08-27T00:00:00Z",
            lastMessageAt: null,
          },
        ],
      });
      // The dock's answer is still running in the shared Assistant chat: the
      // send is queued behind it, on the dock's run.
      sendMessageMock.mockImplementation(
        (body: { message: string }, opts?: { onSuccess?: (m: unknown) => void }) =>
          opts?.onSuccess?.({
            queued: true,
            runId: "run-dock",
            input: {
              id: "q1",
              text: body.message,
              authorName: "Me",
              createdAt: "",
              consumedAt: null,
              consumedStep: null,
              delivery: "next_turn",
            },
          }),
      );
      let liveMessages: unknown[] = [];
      messagesMock.mockImplementation(() => ({ data: liveMessages }));
      const ui = (
        <AgentInstructionsPanel
          agentId="agent-1"
          agentName="Nora"
          mission="Old mission"
          mayManage={true}
        />
      );
      const { rerender, qc } = renderWithClient(ui);

      fireEvent.click(screen.getByRole("button", { name: /draft with copilot/i }));
      fireEvent.change(screen.getByPlaceholderText(/handles first-level it support/i), {
        target: { value: "Answer billing questions" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^generate$/i }));

      await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("/runs/run-dock/inputs/q1"));
      expect(toastError).toHaveBeenCalledWith(
        "The copilot is still answering. Try again when it's done.",
      );
      // Not waiting on the dock's run: its reply is never taken as the draft.
      liveMessages = [
        {
          id: "a9",
          sessionId: "s1",
          role: "assistant",
          content: "The dock's own answer.",
          runId: "run-dock",
          renderedComponents: [],
          createdAt: "2026-08-27T00:00:02Z",
        },
      ];
      rerender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
      expect(screen.queryByText("The dock's own answer.")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /^generate$/i })).not.toBeDisabled();
    });

    it("on a failed generate request, shows an error toast and stops waiting for a reply", async () => {
      assistantMock.mockReturnValue({ data: { agentId: "assistant-1" } });
      sessionsMock.mockReturnValue({
        data: [
          {
            id: "s1",
            agentId: "assistant-1",
            title: "",
            createdAt: "2026-08-27T00:00:00Z",
            lastMessageAt: null,
          },
        ],
      });
      sendMessageMock.mockImplementation(
        (_body: { message: string }, opts?: { onError?: () => void }) => opts?.onError?.(),
      );

      renderWithClient(
        <AgentInstructionsPanel
          agentId="agent-1"
          agentName="Nora"
          mission="Old mission"
          mayManage={true}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /draft with copilot/i }));
      fireEvent.change(screen.getByPlaceholderText(/handles first-level it support/i), {
        target: { value: "Answer billing questions" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^generate$/i }));

      // Failure clears the in-flight state -- Generate is clickable again,
      // not stuck showing "Generating…" forever.
      await waitFor(() =>
        expect(screen.getByRole("button", { name: /^generate$/i })).not.toBeDisabled(),
      );
    });
  });

  describe("Attached files", () => {
    const fileDto = {
      id: "f1",
      filename: "runbook.pdf",
      contentType: "application/pdf",
      sizeBytes: 1024,
      isImage: false,
      createdAt: "2026-09-03T00:00:00Z",
    };

    it("shows no files attached yet when the list is empty", async () => {
      renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={true} />);
      expect(await screen.findByText(/no files attached yet/i)).toBeInTheDocument();
    });

    it("lists files already attached to the agent's instructions", async () => {
      listInstructionFilesMock.mockReturnValue([fileDto]);
      renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={true} />);
      expect(await screen.findByText("runbook.pdf")).toBeInTheDocument();
    });

    it("shows an error instead of silently rendering empty when the list fails to load", async () => {
      // Regression: a view-only caller used to 403 on this query with no
      // error branch in the UI, so the section just looked empty -- worse
      // than an explicit "couldn't load" message. Covers any other failure
      // mode too (network error, 500, etc.), not just the auth-gate bug.
      listInstructionFilesMock.mockRejectedValue(new Error("forbidden"));
      renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={true} />);
      expect(await screen.findByText(/couldn't load attached files/i)).toBeInTheDocument();
      expect(screen.queryByText(/no files attached yet/i)).not.toBeInTheDocument();
    });

    it("uploading a file sends it for this agent and adds it to the list", async () => {
      let uploaded = false;
      uploadInstructionFileMock.mockImplementation(async (agentId: string, file: File) => {
        expect(agentId).toBe("agent-1");
        expect(file.name).toBe("runbook.pdf");
        uploaded = true;
        return fileDto;
      });
      listInstructionFilesMock.mockImplementation(() => (uploaded ? [fileDto] : []));

      renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={true} />);
      await screen.findByText(/no files attached yet/i);

      const file = new File(["%PDF-1.4"], "runbook.pdf", { type: "application/pdf" });
      fireEvent.change(screen.getByLabelText(/attach a file/i), { target: { files: [file] } });

      await waitFor(() => expect(uploadInstructionFileMock).toHaveBeenCalledWith("agent-1", file));
      expect(await screen.findByText("runbook.pdf")).toBeInTheDocument();
    });

    it("refuses a file over the 25 MB cap without calling the upload", async () => {
      // Client half of the cap: fast feedback only -- the server's own check
      // is authoritative.
      listInstructionFilesMock.mockImplementation(() => []);
      renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={true} />);
      await screen.findByText(/no files attached yet/i);

      const huge = new File(["%PDF-1.4"], "huge.pdf", { type: "application/pdf" });
      Object.defineProperty(huge, "size", { value: 26 * 1024 * 1024 });
      fireEvent.change(screen.getByLabelText(/attach a file/i), { target: { files: [huge] } });

      expect(uploadInstructionFileMock).not.toHaveBeenCalled();
    });

    it("deleting an attached file asks for confirmation, then removes it", async () => {
      let deleted = false;
      listInstructionFilesMock.mockImplementation(() => (deleted ? [] : [fileDto]));
      deleteFileMock.mockImplementation(async (fileId: string) => {
        expect(fileId).toBe("f1");
        deleted = true;
      });

      renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={true} />);
      await screen.findByText("runbook.pdf");

      fireEvent.click(screen.getByRole("button", { name: /delete file/i }));
      // Confirming must not have persisted yet -- the dialog is a real gate.
      expect(deleteFileMock).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));

      await waitFor(() => expect(deleteFileMock).toHaveBeenCalledWith("f1"));
      await waitFor(() => expect(screen.queryByText("runbook.pdf")).not.toBeInTheDocument());
    });

    it("cancelling the delete confirmation leaves the file and never persists", async () => {
      listInstructionFilesMock.mockReturnValue([fileDto]);
      renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={true} />);
      await screen.findByText("runbook.pdf");

      fireEvent.click(screen.getByRole("button", { name: /delete file/i }));
      fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

      expect(deleteFileMock).not.toHaveBeenCalled();
      expect(screen.getByText("runbook.pdf")).toBeInTheDocument();
    });

    it("hides the attach button and delete controls when the caller lacks agent:manage", async () => {
      listInstructionFilesMock.mockReturnValue([fileDto]);
      renderWithClient(<AgentInstructionsPanel agentId="agent-1" mission="" mayManage={false} />);
      await screen.findByText("runbook.pdf");

      expect(screen.queryByLabelText(/attach a file/i)).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /delete file/i })).not.toBeInTheDocument();
    });
  });
});
