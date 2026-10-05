import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({ mobile: false }));

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (opts: unknown) => opts,
}));
vi.mock("@/hooks/use-mobile", () => ({ useIsMobile: () => m.mobile }));
vi.mock("@/lib/governance-hooks", () => ({ useMay: () => () => true }));
vi.mock("@/lib/hooks-chat", () => ({ useChatSessions: () => ({ data: [] }) }));
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
vi.mock("@/components/chat-window", () => ({
  ChatWindow: () => <div data-testid="chat" />,
  ChatSessionPicker: () => <div data-testid="picker" />,
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
});

describe("CopilotPage", () => {
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
});
