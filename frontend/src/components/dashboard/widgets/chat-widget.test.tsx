import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatWidget } from "./chat-widget";
import * as governanceHooks from "@/lib/governance-hooks";
import * as hooks from "@/lib/hooks";
import * as liveProvider from "@/lib/live/provider";

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return { ...actual, useAssistant: () => ({ data: { agentId: "agent-1" } }) };
});
const chatSessions = vi.hoisted(() => ({ data: [] as unknown[] }));
vi.mock("@/lib/hooks-chat", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-chat")>();
  return {
    ...actual,
    useChatSessions: () => ({ data: chatSessions.data }),
  };
});

vi.mock("@/components/conversation", () => ({
  Conversation: (p: {
    agentId: string;
    sessionId: string | null;
    onSessionChange: (id: string) => void;
    variant: string;
  }) => (
    <div data-testid="conversation" data-agent={p.agentId} data-variant={p.variant}>
      {p.sessionId}
      <button type="button" onClick={() => p.onSessionChange("new-session-1")}>
        create
      </button>
    </div>
  ),
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("ChatWidget", () => {
  // `vi.spyOn(...).mockReturnValue(...)` otherwise leaks into later tests in
  // this file -- there is no global mock-reset config (vitest.setup.ts).
  afterEach(() => {
    vi.restoreAllMocks();
    chatSessions.data = [];
  });

  const older = "0190f2a0-0000-7000-8000-00000000000a";
  const newer = "0190f2a0-0000-7000-8000-00000000000b";
  const twoChats = [
    {
      id: older,
      agentId: "agent-1",
      title: "Old",
      createdAt: "t",
      lastMessageAt: "2026-11-01T00:00:00Z",
    },
    {
      id: newer,
      agentId: "agent-1",
      title: "Testchat E2E",
      createdAt: "t",
      lastMessageAt: "2026-11-08T00:00:00Z",
    },
  ];

  it("opens the most recent chat when the tile was never pointed at one (E2E E)", () => {
    chatSessions.data = twoChats;
    render(<ChatWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByTestId("conversation")).toHaveTextContent(newer);
    expect(screen.getByRole("button", { name: "Switch chat" })).toHaveTextContent("Testchat E2E");
  });

  it("keeps a new chat new, and labels the picker New chat", () => {
    chatSessions.data = twoChats;
    render(<ChatWidget config={{ sessionId: null }} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByTestId("conversation")).toHaveTextContent(/^create$/);
    expect(screen.getByRole("button", { name: "Switch chat" })).toHaveTextContent("New chat");
  });

  it("mounts the one conversation for a tile with no session yet", () => {
    render(<ChatWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    const conversation = screen.getByTestId("conversation");
    expect(conversation).toHaveAttribute("data-agent", "agent-1");
    expect(conversation).toHaveAttribute("data-variant", "compact");
    expect(conversation).toHaveTextContent(/^create$/);
  });

  it("binds the conversation to the tile's session without writing config on mount", () => {
    const onConfigChange = vi.fn();
    const sid = "0190f2a0-0000-7000-8000-000000000001";
    render(<ChatWidget config={{ sessionId: sid }} onConfigChange={onConfigChange} />, {
      wrapper,
    });
    expect(screen.getByTestId("conversation")).toHaveTextContent(sid);
    expect(onConfigChange).not.toHaveBeenCalled();
  });

  it("never puts a stored session id that is not a UUID into a path", () => {
    render(<ChatWidget config={{ sessionId: "../members" }} onConfigChange={vi.fn()} />, {
      wrapper,
    });
    expect(screen.getByTestId("conversation")).not.toHaveTextContent("../members");
  });

  it("writes the newly created session id back into config", () => {
    const onConfigChange = vi.fn();
    render(<ChatWidget config={{}} onConfigChange={onConfigChange} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: "create" }));
    expect(onConfigChange).toHaveBeenCalledWith({ sessionId: "new-session-1" });
  });

  it("renders a fallback instead of the composer for a member without copilot:use", () => {
    vi.spyOn(governanceHooks, "useCan").mockReturnValue(() => false);

    render(<ChatWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });

    expect(screen.queryByTestId("conversation")).not.toBeInTheDocument();
    expect(screen.getByText(/don't have access to copilot chat/i)).toBeInTheDocument();
  });

  it("shows a reconnecting notice when the live connection drops, matching CopilotDock", () => {
    vi.spyOn(liveProvider, "useLiveConnectionStatus").mockReturnValue("disconnected");

    render(<ChatWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });

    expect(screen.getByText(/new messages may be delayed/i)).toBeInTheDocument();
  });

  it("surfaces a pending copilot proposal with Apply/Reject, matching CopilotDock", () => {
    vi.spyOn(hooks, "useCopilotProposals").mockReturnValue({
      data: [
        {
          id: "p1",
          status: "draft",
          operations: [{ label: "create_department", references: {} }],
        },
      ],
    } as never);
    vi.spyOn(hooks, "useApplyCopilotProposal").mockReturnValue({
      isPending: false,
      mutate: vi.fn(),
    } as never);
    vi.spyOn(hooks, "useRejectCopilotProposal").mockReturnValue({
      isPending: false,
      mutate: vi.fn(),
    } as never);

    render(<ChatWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });

    expect(screen.getByText(/pending proposals/i)).toBeInTheDocument();
    expect(screen.getByText("create_department")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });
});
