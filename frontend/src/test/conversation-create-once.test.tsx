// frontend/src/test/conversation-create-once.test.tsx
// A new chat is created once, however many callers need it at the same time
// (a multi-file drop, a send while files upload).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const upload = vi.hoisted(() => vi.fn());
const create = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  uploadChatAttachment: upload,
}));
vi.mock("@/components/conversation/use-conversation", () => ({
  useConversation: () => ({
    session: undefined,
    messages: [],
    turns: [],
    isLoading: false,
    liveRunIds: [],
    addPendingRun: vi.fn(),
  }),
}));
vi.mock("@/lib/hooks-chat", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks-chat")>()),
  useCreateChatSession: () => ({ mutateAsync: create }),
  useSendChatMessage: () => ({ mutateAsync: vi.fn() }),
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
vi.mock("@/lib/governance-hooks", () => ({ useCan: () => () => true }));

import { Conversation } from "@/components/conversation";

function attachment(id: string, filename: string) {
  return {
    id,
    filename,
    contentType: "application/pdf",
    sizeBytes: 3,
    isImage: false,
    createdAt: "",
  };
}

describe("Conversation session creation", () => {
  it("creates the chat once for two files dropped on a new chat", async () => {
    create.mockResolvedValue({ id: "new", agentId: "a" });
    upload.mockImplementation(async (_sessionId: string, file: File) =>
      attachment(`att-${file.name}`, file.name),
    );
    const onSessionChange = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation
          agentId="a"
          sessionId={null}
          onSessionChange={onSessionChange}
          variant="full"
        />
      </QueryClientProvider>,
    );
    const box = screen.getByRole("textbox", { name: /Message/ });
    const a = new File(["abc"], "a.pdf", { type: "application/pdf" });
    const b = new File(["abc"], "b.pdf", { type: "application/pdf" });
    fireEvent.drop(box, { dataTransfer: { files: [a] } });
    fireEvent.drop(box, { dataTransfer: { files: [b] } });
    await waitFor(() => expect(screen.getByText("b.pdf")).toBeInTheDocument());
    expect(screen.getByText("a.pdf")).toBeInTheDocument();
    expect(create).toHaveBeenCalledTimes(1);
    expect(onSessionChange).toHaveBeenCalledTimes(1);
    expect(upload).toHaveBeenCalledWith("new", a);
    expect(upload).toHaveBeenCalledWith("new", b);
  });

  it("focuses the message box on mount when asked to", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation
          agentId="a"
          sessionId="s"
          onSessionChange={vi.fn()}
          variant="compact"
          autoFocus
        />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("textbox", { name: /Message/ })).toHaveFocus();
  });

  it("leaves a typed draft alone when a prompt starter is sent", async () => {
    create.mockClear();
    create.mockReturnValue(new Promise(() => undefined));
    const onDraftChange = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Conversation
          agentId="a"
          sessionId={null}
          onSessionChange={vi.fn()}
          variant="full"
          draft="half a thought"
          onDraftChange={onDraftChange}
          emptyState={(send) => (
            <button type="button" onClick={() => send("Starter")}>
              Starter
            </button>
          )}
        />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByText("Starter"));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(onDraftChange).not.toHaveBeenCalled();
  });
});
