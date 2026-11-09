// frontend/src/test/conversation-live-layout.test.tsx
// The Live tab sits on a page that scrolls as a whole. It must bring its own
// bounded height, so the conversation (not the page) scrolls, the composer
// stays in view under it, and scrolling up loads older items (I1).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const fetchOlder = vi.hoisted(() => vi.fn());
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
  useCallerCapabilities: () => null,
  useCan: () => () => true,
  useMay: () => () => false,
  useMayManageAgent: () => () => false,
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useLiveTimeline: () => ({
    data: { sessionId: "live-1", mayViewAllRuns: false, nextBefore: "c", items: [] },
    error: null,
    hasOlder: true,
    loadingOlder: false,
    fetchOlder,
  }),
  useRunSteering: () => ({ data: undefined }),
  useSessionModels: () => ({ data: [], isLoading: false }),
}));

import { LiveTimeline } from "@/components/conversation/live-timeline";

describe("Live tab layout", () => {
  it("scrolls inside a bounded frame with the composer below the scroller", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <LiveTimeline agentId="a" departmentId={null} promptStarters={[]} />
      </QueryClientProvider>,
    );
    const frame = screen.getByTestId("live-frame");
    // A bounded height of its own, not h-full of an auto-height page block.
    expect(frame.className).toMatch(/\bh-\[calc\(100dvh-[^\]]+\)\]/);
    expect(frame.className).toMatch(/\bmin-h-\[/);
    expect(frame.className).toMatch(/\bflex-col\b/);
    expect(frame.className).toMatch(/\boverflow-hidden\b/);

    const scroller = screen.getByTestId("conversation-scroller");
    expect(frame.contains(scroller)).toBe(true);
    expect(scroller.className).toMatch(/\bmin-h-0\b/);
    expect(scroller.className).toMatch(/\bflex-1\b/);
    expect(scroller.className).toMatch(/\boverflow-y-auto\b/);
    // Every element between frame and scroller is a flex column that may shrink.
    for (let el = scroller.parentElement; el && el !== frame; el = el.parentElement) {
      expect(el.className).toMatch(/\bflex\b/);
      expect(el.className).toMatch(/\bmin-h-0\b/);
    }
    // The composer is outside the scroller: it stays in view.
    const box = screen.getByRole("textbox", { name: /Message/ });
    expect(scroller.contains(box)).toBe(false);
    expect(frame.contains(box)).toBe(true);

    // Scrolling near the top loads older items.
    Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(scroller, "clientHeight", { configurable: true, value: 500 });
    scroller.scrollTop = 40;
    fireEvent.scroll(scroller);
    expect(fetchOlder).toHaveBeenCalled();
  });
});
