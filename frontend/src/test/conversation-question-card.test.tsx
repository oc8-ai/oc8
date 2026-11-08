// frontend/src/test/conversation-question-card.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const answer = vi.hoisted(() => vi.fn());
const answerAsync = vi.hoisted(() => vi.fn());
const session = vi.hoisted(() => ({ current: undefined as unknown }));
const upload = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  uploadChatAttachment: upload,
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useAnswerQuestion: () => ({ mutate: answer, mutateAsync: answerAsync, isPending: false }),
}));
vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({
    session: session.current,
    messages: [],
    turns: [],
    isLoading: false,
    liveRunIds: ["r1"],
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
  // No approval:view: the chat sends no approvals request.
  useMay: () => () => false,
}));

import { Conversation } from "@/components/conversation";
import { QuestionCard, QuestionSummary } from "@/components/conversation/question-card";
import { UserTurn } from "@/components/conversation/user-turn";
import { ApiError } from "@/lib/api";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

const pending = {
  clarificationId: "c1",
  runId: "r1",
  text: "",
  questions: [
    {
      id: "region",
      prompt: "Which region?",
      kind: "single",
      allowOther: true,
      required: true,
      options: [
        { key: "eu", label: "EU", detail: "" },
        { key: "us", label: "US", detail: "" },
      ],
    },
    {
      id: "channels",
      prompt: "Which channels?",
      kind: "multi",
      allowOther: false,
      required: false,
      options: [
        { key: "mail", label: "Mail", detail: "" },
        { key: "sms", label: "SMS", detail: "" },
        { key: "push", label: "Push", detail: "" },
      ],
    },
  ],
};

beforeEach(() => {
  answer.mockReset();
  answerAsync.mockReset();
});

describe("QuestionCard", () => {
  it("navigates questions and picks with digits and Space", () => {
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={vi.fn()} />);
    expect(screen.getByRole("region", { name: /Questions/ })).toBeInTheDocument();
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    const card = screen.getByRole("group", { name: /Which region\?/ });
    expect(screen.getByRole("radiogroup", { name: /Which region\?/ })).toBeInTheDocument();
    fireEvent.keyDown(card, { key: "2" });
    expect(screen.getByRole("radio", { name: "US" })).toHaveAttribute("aria-checked", "true");
    fireEvent.keyDown(card, { key: "Enter" });
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
    const second = screen.getByRole("group", { name: /Which channels\?/ });
    fireEvent.keyDown(second, { key: "1" });
    fireEvent.keyDown(second, { key: "ArrowDown" });
    fireEvent.keyDown(second, { key: "ArrowDown" });
    fireEvent.keyDown(second, { key: " " });
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    fireEvent.keyDown(second, { key: "Enter" });
    expect(answer).toHaveBeenCalledWith(
      { clarificationId: "c1", answers: { region: ["us"], channels: ["mail", "push"] } },
      expect.anything(),
    );
  });

  it("moves between questions with ArrowLeft and ArrowRight", () => {
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={vi.fn()} />);
    fireEvent.keyDown(screen.getByRole("group", { name: /Which region\?/ }), { key: "ArrowRight" });
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("group", { name: /Which channels\?/ }), {
      key: "ArrowLeft",
    });
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
  });

  it("accepts an other answer as text, and digits typed there stay text", () => {
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("radio", { name: "Other answer" }));
    const field = screen.getByRole("textbox", { name: "Other answer" });
    expect(field).toHaveAttribute("maxLength", "2000");
    fireEvent.keyDown(field, { key: "1" });
    expect(screen.getByRole("radio", { name: "EU" })).toHaveAttribute("aria-checked", "false");
    fireEvent.change(field, { target: { value: "Switzerland" } });
    fireEvent.click(screen.getByRole("button", { name: /Next question/ }));
    fireEvent.click(screen.getByRole("button", { name: /Send answers/ }));
    expect(answer).toHaveBeenLastCalledWith(
      { clarificationId: "c1", answers: { region: "Switzerland" } },
      expect.anything(),
    );
  });

  it("does not send while a required question is unanswered", () => {
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Send answers/ }));
    expect(answer).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("This question needs an answer.");
  });

  it("answers a plain question with free text", () => {
    const plain = { clarificationId: "c2", runId: "r2", text: "Which budget?", questions: [] };
    render(<QuestionCard sessionId="s" pending={plain} onClose={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Which budget?" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Your answer" }), {
      target: { value: "5000 EUR" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send answers/ }));
    expect(answer).toHaveBeenCalledWith(
      { clarificationId: "c2", text: "5000 EUR" },
      expect.anything(),
    );
  });

  it("shows a 422 inline and keeps the card open", () => {
    answer.mockImplementation((_v, opts: { onError: (e: unknown) => void }) =>
      opts.onError(new ApiError("answer is not allowed", 422)),
    );
    const onClose = vi.fn();
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={onClose} />);
    fireEvent.click(screen.getByRole("radio", { name: "EU" }));
    fireEvent.click(screen.getByRole("button", { name: /Send answers/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("answer is not allowed");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes with a notice on 409 and silently on 404", () => {
    const onClose = vi.fn();
    answer.mockImplementationOnce((_v, opts: { onError: (e: unknown) => void }) =>
      opts.onError(new ApiError("already answered", 409)),
    );
    const { unmount } = render(
      <QuestionCard sessionId="s" pending={pending as never} onClose={onClose} />,
    );
    fireEvent.click(screen.getByRole("radio", { name: "EU" }));
    fireEvent.click(screen.getByRole("button", { name: /Send answers/ }));
    expect(onClose).toHaveBeenLastCalledWith("Already answered");
    unmount();
    answer.mockImplementationOnce((_v, opts: { onError: (e: unknown) => void }) =>
      opts.onError(new ApiError("question not found", 404)),
    );
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={onClose} />);
    fireEvent.click(screen.getByRole("radio", { name: "EU" }));
    fireEvent.click(screen.getByRole("button", { name: /Send answers/ }));
    expect(onClose).toHaveBeenLastCalledWith();
  });

  it("sends once on a double Enter", () => {
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("radio", { name: "EU" }));
    fireEvent.click(screen.getByRole("button", { name: /Next question/ }));
    const second = screen.getByRole("group", { name: /Which channels\?/ });
    fireEvent.keyDown(second, { key: "Enter" });
    fireEvent.keyDown(second, { key: "Enter" });
    fireEvent.keyDown(second, { key: "Enter", repeat: true });
    expect(answer).toHaveBeenCalledTimes(1);
  });

  it("ignores Enter while an IME is composing", () => {
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={vi.fn()} />);
    fireEvent.keyDown(screen.getByRole("group", { name: /Which region\?/ }), {
      key: "Enter",
      isComposing: true,
    });
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
  });

  it("keeps Space from clicking an option a second time on keyup", () => {
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={vi.fn()} />);
    const eu = screen.getByRole("radio", { name: "EU" });
    expect(fireEvent.keyUp(eu, { key: " " })).toBe(false);
  });

  it("never mixes ticks and an own answer on a multi question", () => {
    const multi = {
      ...pending,
      questions: [{ ...pending.questions[1], allowOther: true, required: true }],
    };
    render(<QuestionCard sessionId="s" pending={multi as never} onClose={vi.fn()} />);
    expect(screen.getByText("Your own answer replaces the ticked options.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Mail" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Other answer" }));
    expect(screen.getByRole("checkbox", { name: "Mail" })).toHaveAttribute("aria-checked", "false");
    fireEvent.change(screen.getByRole("textbox", { name: "Other answer" }), {
      target: { value: "Fax" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "SMS" }));
    expect(screen.getByRole("checkbox", { name: "Other answer" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(screen.queryByRole("textbox", { name: "Other answer" })).not.toBeInTheDocument();
    // The cleared own answer does not come back when Other is picked again.
    fireEvent.click(screen.getByRole("checkbox", { name: "Other answer" }));
    expect(screen.getByRole("textbox", { name: "Other answer" })).toHaveValue("");
    fireEvent.click(screen.getByRole("checkbox", { name: "SMS" }));
    fireEvent.click(screen.getByRole("button", { name: /Send answers/ }));
    expect(answer).toHaveBeenCalledWith(
      { clarificationId: "c1", answers: { channels: ["sms"] } },
      expect.anything(),
    );
  });

  it("closes with the close button", () => {
    const onClose = vi.fn();
    render(<QuestionCard sessionId="s" pending={pending as never} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: /Close questions/ }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("QuestionSummary", () => {
  it("is one line of prompts and the labels picked", () => {
    render(
      <QuestionSummary
        questions={pending.questions}
        answers={{ region: "Switzerland", channels: ["mail", "push"] }}
      />,
    );
    expect(screen.getByText("Switzerland")).toBeInTheDocument();
    expect(screen.getByText("Mail, Push")).toBeInTheDocument();
  });
});

describe("UserTurn with an answers part", () => {
  const message: ChatMessageDTO = {
    id: "u9",
    sessionId: "s",
    role: "user",
    content: "Which region?: US",
    runId: null,
    renderedComponents: [],
    createdAt: "2026-11-08T09:00:00Z",
    attachments: [],
    mode: null,
    contextRefs: [],
    authorMemberId: null,
    authorName: null,
    replyToId: null,
    parts: [
      {
        type: "answers",
        // Stored unchanged from the backend, so snake_case.
        questions: [
          {
            id: "region",
            prompt: "Which region?",
            kind: "single",
            options: [{ key: "us", label: "US", detail: "" }],
            allow_other: true,
            required: true,
          },
        ],
        answers: { region: ["us"] },
      },
    ],
  };

  it("renders the summary instead of the raw content, also without an author", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <UserTurn
          turn={{ kind: "user", key: "u9", message }}
          viewerMemberId="me"
          canEdit
          onEdit={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText("US")).toBeInTheDocument();
    expect(screen.queryByText("Which region?: US")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit and resend/ })).not.toBeInTheDocument();
  });
});

describe("Conversation with a parked question", () => {
  function mount() {
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation agentId="a" sessionId="s" onSessionChange={vi.fn()} variant="full" />
      </QueryClientProvider>,
    );
  }

  it("shows the card, reopens it after closing, and answers freely from the composer", async () => {
    session.current = {
      id: "s",
      readOnly: false,
      activeRuns: [{ runId: "r1", replyToId: null, state: "waiting_for_input" }],
      pendingQuestion: pending,
      modelOverrideId: null,
    };
    answerAsync.mockResolvedValue({ runId: "r1" });
    mount();
    expect(screen.getByRole("region", { name: "Questions" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Close questions/ }));
    expect(screen.queryByRole("region", { name: "Questions" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show questions" }));
    expect(screen.getByRole("region", { name: "Questions" })).toBeInTheDocument();
    // The parked run does not make the chat busy: the composer sends.
    const box = screen.getByRole("textbox", { name: /Message/ });
    expect(box).toHaveAttribute("placeholder", "Or answer freely …");
    fireEvent.change(box, { target: { value: "EU, but only mail" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() =>
      expect(answerAsync).toHaveBeenCalledWith({
        clarificationId: "c1",
        text: "EU, but only mail",
      }),
    );
  });

  it("says when the question was already answered", async () => {
    session.current = {
      id: "s",
      readOnly: false,
      activeRuns: [],
      pendingQuestion: pending,
      modelOverrideId: null,
    };
    answerAsync.mockRejectedValue(new ApiError("already answered", 409));
    mount();
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "EU" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(answerAsync).toHaveBeenCalled());
    // The card stays until the refetch clears pendingQuestion; no error shows,
    // and the typed text is back to be sent as an ordinary message.
    expect(screen.queryByText(/was not sent/)).not.toBeInTheDocument();
    await waitFor(() => expect(box).toHaveValue("EU"));
  });

  it("answers as text only: no + menu, and chips are never dropped", async () => {
    upload.mockResolvedValue({
      id: "att1",
      filename: "brief.pdf",
      contentType: "application/pdf",
      sizeBytes: 3,
      isImage: false,
      createdAt: "",
    });
    session.current = {
      id: "s",
      readOnly: false,
      activeRuns: [],
      pendingQuestion: null,
      modelOverrideId: null,
    };
    const view = mount();
    const box = screen.getByRole("textbox", { name: /Message/ });
    const file = new File(["abc"], "brief.pdf", { type: "application/pdf" });
    fireEvent.paste(box, { clipboardData: { files: [file], items: [] } });
    await waitFor(() => expect(screen.getByText("brief.pdf")).toBeInTheDocument());

    // The agent parks on a question while the file is attached.
    session.current = {
      id: "s",
      readOnly: false,
      activeRuns: [],
      pendingQuestion: pending,
      modelOverrideId: null,
    };
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation agentId="a" sessionId="s" onSessionChange={vi.fn()} variant="full" />
      </QueryClientProvider>,
    );
    expect(
      screen.getByText("Sent as the answer – attachments only afterwards."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add files or context" })).toBeDisabled();

    fireEvent.change(box, { target: { value: "EU" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(answerAsync).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("An answer is sent as text only.");
    expect(box).toHaveValue("EU");
    expect(screen.getByText("brief.pdf")).toBeInTheDocument();

    // A file pasted while the question is open is refused, not uploaded.
    upload.mockClear();
    fireEvent.paste(box, { clipboardData: { files: [file], items: [] } });
    expect(upload).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("once the question is answered");

    // Removing the chip lets the answer go out.
    fireEvent.click(screen.getByRole("button", { name: "Remove brief.pdf" }));
    answerAsync.mockResolvedValue({ runId: "r1" });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() =>
      expect(answerAsync).toHaveBeenCalledWith({ clarificationId: "c1", text: "EU" }),
    );
  });
});
