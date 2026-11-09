import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({
  mobile: false,
  sessions: [] as Array<{ id: string; agentId: string }>,
  linked: undefined as { id: string; agentId: string } | undefined,
  sent: [] as string[],
}));

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (opts: unknown) => opts,
  useRouter: () => null,
}));
vi.mock("@/lib/conversation-api", () => ({
  useChatSession: (id: string | null) => ({ data: id ? m.linked : undefined }),
}));
vi.mock("@/lib/hooks", () => ({
  useAgent: (id: string) => ({ data: { id, name: "Stock agent" } }),
}));
vi.mock("@/lib/agent-builder-api", () => ({
  useAgentIdeas: () => ({
    data: [
      {
        key: "invoice_checker",
        title: { en: "Invoice checker", de: "Rechnungsprüfer" },
        subtitle: {
          en: "Checks open invoices every morning",
          de: "Prüft jeden Morgen offene Rechnungen",
        },
        prompt: {
          en: "/agent Set up an invoice checker.",
          de: "/agent Leg mir einen Rechnungsprüfer an.",
        },
      },
    ],
  }),
}));
vi.mock("@/hooks/use-mobile", () => ({ useIsMobile: () => m.mobile }));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useMay: () => () => true,
}));
vi.mock("@/lib/hooks-chat", () => ({ useChatSessions: () => ({ data: m.sessions }) }));
vi.mock("@/lib/hooks-copilot", () => ({
  useCopilotProfile: () => ({
    data: {
      displayName: "Pip",
      avatar: { shape: "round", color: "indigo" },
      pausedAt: null,
      status: "waiting",
      activeCount: 3,
      agentId: "agent-1",
    },
  }),
  usePauseCopilot: () => ({ mutate: vi.fn(), isPending: false }),
  useResumeCopilot: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/conversation", () => ({
  Conversation: ({
    sessionId,
    onSessionChange,
    emptyState,
    promptStarters = [],
  }: {
    sessionId: string | null;
    onSessionChange: (id: string) => void;
    emptyState?: React.ReactNode | ((send: (text: string) => void) => React.ReactNode);
    promptStarters?: { title: string; subtitle: string; prompt?: string }[];
  }) => (
    <div data-testid="chat" data-session={sessionId ?? ""}>
      <button type="button" onClick={() => onSessionChange("s-8")}>
        switch
      </button>
      {typeof emptyState === "function" ? emptyState((text) => m.sent.push(text)) : emptyState}
      {promptStarters.map((s) => (
        <button key={s.title} type="button" onClick={() => m.sent.push(s.prompt ?? s.title)}>
          {s.title}
        </button>
      ))}
    </div>
  ),
}));
vi.mock("@/components/conversation/copilot-sidebar", () => ({
  CopilotSidebar: ({ onSelect }: { onSelect: (id: string | null) => void }) => (
    <nav aria-label="Chats">
      <button type="button" onClick={() => onSelect("s-sidebar")}>
        sidebar pick
      </button>
    </nav>
  ),
}));
vi.mock("@/components/copilot-work-panel", () => ({
  CopilotWorkPanel: () => <div data-testid="work-panel" />,
}));

import { CopilotPage } from "@/routes/copilot";

function wrap() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <CopilotPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  m.mobile = false;
  m.sessions = [];
  m.linked = undefined;
  m.sent = [];
});

describe("CopilotPage", () => {
  it("opens the Copilot's own session a /copilot?session= link names", () => {
    m.sessions = [{ id: "s-7", agentId: "agent-1" }];
    render(
      <QueryClientProvider client={new QueryClient()}>
        <CopilotPage initialSessionId="s-7" />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "s-7");
  });

  it("adopts a linked session it learns belongs to the Copilot", () => {
    m.linked = { id: "s-7", agentId: "agent-1" };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <CopilotPage initialSessionId="s-7" />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "s-7");
  });

  it("does not open another agent's conversation and links to that agent instead", () => {
    m.linked = { id: "s-9", agentId: "agent-2" };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <CopilotPage initialSessionId="s-9" />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "");
    expect(screen.getByText(/This chat belongs to/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Stock agent" })).toHaveAttribute(
      "href",
      "/agents/agent-2",
    );
  });

  it("re-selects the linked session when the same link is followed again", () => {
    m.sessions = [{ id: "s-7", agentId: "agent-1" }];
    const ui = (navKey: string) => (
      <QueryClientProvider client={new QueryClient()}>
        <CopilotPage initialSessionId="s-7" navKey={navKey} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui("k1"));
    fireEvent.click(screen.getByRole("button", { name: "switch" }));
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "s-8");
    rerender(ui("k2"));
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "s-7");
  });

  it("desktop shows the persona header, the chat and the inline work panel", () => {
    wrap();
    expect(screen.getByText("Pip")).toBeInTheDocument();
    expect(screen.getByTestId("chat")).toBeInTheDocument();
    expect(screen.getByTestId("work-panel").closest("aside")).not.toBeNull();
  });

  it("mobile hides the aside and opens the panel in a bottom sheet", () => {
    m.mobile = true;
    wrap();
    expect(screen.queryByTestId("work-panel")).toBeNull();
    const btn = screen.getByRole("button", { name: /Work/ });
    expect(btn).toHaveTextContent("3");
    fireEvent.click(btn);
    const panel = screen.getByTestId("work-panel");
    expect(panel.closest("aside")).toBeNull();
    expect(panel.closest('[role="dialog"]')).not.toBeNull();
  });

  it("replaces the Chats picker with the sidebar and selects from it", () => {
    wrap();
    expect(screen.queryByTestId("picker")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "sidebar pick" }));
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "s-sidebar");
  });

  it("replaces the address with the chat picked in the sidebar or created by the chat", () => {
    const opened = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <CopilotPage onSessionOpened={opened} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "sidebar pick" }));
    expect(opened).toHaveBeenLastCalledWith("s-sidebar");
    fireEvent.click(screen.getByRole("button", { name: "switch" }));
    expect(opened).toHaveBeenLastCalledWith("s-8");
    expect(opened).toHaveBeenCalledTimes(2);
  });

  it("sends a prompt starter from the empty chat", () => {
    wrap();
    fireEvent.click(screen.getByRole("button", { name: "What is waiting on me?" }));
    expect(m.sent).toEqual(["What is waiting on me?"]);
  });

  it("puts agent ideas in front of the usual starters", () => {
    wrap();
    const buttons = screen.getAllByRole("button").map((b) => b.textContent);
    expect(buttons.indexOf("Invoice checker")).toBeLessThan(
      buttons.indexOf("Keep an eye on something for me"),
    );
    fireEvent.click(screen.getByText("Invoice checker"));
    expect(m.sent).toContain("/agent Set up an invoice checker.");
  });
});
