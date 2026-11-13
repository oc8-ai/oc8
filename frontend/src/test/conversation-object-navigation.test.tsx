// Object chips in an answer open their object in-app, the streaming caret
// sits inline after the last word, and the chip targets select the object.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// ChatMarkdown's record-link check, without a server: no connected hosts, so
// every http(s) link is an ordinary one.
vi.mock("@/lib/record-links", () => ({
  useConnectedHosts: () => ({ data: new Set<string>() }),
  useVerifiedLinks: () => ({ data: new Set<string>() }),
}));

const r = vi.hoisted(() => ({ navigate: vi.fn(), search: {} as Record<string, unknown> }));

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (opts: Record<string, unknown>) => ({
    ...opts,
    options: opts,
    useSearch: () => r.search,
  }),
  useRouter: () => ({ navigate: r.navigate }),
  useNavigate: () => r.navigate,
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/lib/hooks", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks")>()),
  useActivity: () => ({ data: [] }),
  useAgents: () => ({ data: { items: [{ id: "a1", name: "Stock agent" }] } }),
  useRun: (runId: string | null) => ({
    data: runId
      ? { id: runId, agentId: "a1", state: "done", steps: 0, toolCalls: [], output: "All counted" }
      : undefined,
  }),
}));

import { ChatMarkdown } from "@/components/chat-markdown";
import { AssistantTurn } from "@/components/conversation/assistant-turn";
import { StreamingText } from "@/components/conversation/streaming-text";
import type { ChatMessageDTO } from "@/lib/hooks-chat";
import { ActivityPage, Route as ActivityRoute } from "@/routes/activity";
import { Route as CopilotRoute } from "@/routes/copilot";

function wrap(ui: React.ReactElement) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  r.navigate.mockReset();
});

describe("object chips", () => {
  it("hand a plain left click to onObjectClick and keep the href for a new tab", () => {
    const onObjectClick = vi.fn();
    render(<ChatMarkdown text="See [Stock](oc8://agent/a1)." onObjectClick={onObjectClick} />);
    const chip = screen.getByRole("link", { name: "Stock" });
    expect(chip).toHaveAttribute("href", "/agents/a1");
    const plain = fireEvent.click(chip, { button: 0 });
    expect(plain).toBe(false); // default prevented
    expect(onObjectClick).toHaveBeenCalledWith("/agents/a1");
    onObjectClick.mockClear();
    expect(fireEvent.click(chip, { button: 0, metaKey: true })).toBe(true);
    expect(fireEvent.click(chip, { button: 0, ctrlKey: true })).toBe(true);
    expect(fireEvent.click(chip, { button: 1 })).toBe(true);
    expect(onObjectClick).not.toHaveBeenCalled();
  });

  it("navigate in-app from an answer through the router", () => {
    const base: ChatMessageDTO = {
      id: "a1",
      sessionId: "s",
      role: "assistant",
      content: "Ask [the run](oc8://run/r-9).",
      runId: null,
      renderedComponents: [],
      createdAt: "2026-11-08T09:00:00Z",
      attachments: [],
      mode: null,
      contextRefs: [],
      authorMemberId: null,
      authorName: null,
      replyToId: "u1",
    };
    wrap(
      <AssistantTurn
        sessionId="s"
        turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [base], liveRunId: null }}
      />,
    );
    fireEvent.click(screen.getByRole("link", { name: "the run" }), { button: 0 });
    expect(r.navigate).toHaveBeenCalledWith({ href: "/activity?run=r-9" });
  });
});

describe("the streaming caret", () => {
  it("is drawn by the markdown root after its last word, not as a block of its own", () => {
    const { container, rerender } = render(<StreamingText text={"One\n\nTwo words"} streaming />);
    const host = screen.getByTestId("stream-tail");
    expect(host).toHaveClass("stream-tail");
    // The caret host is the markdown root itself: its last child is the last
    // paragraph, so the ::after lands inline after "words".
    expect(host.lastElementChild?.textContent).toBe("Two words");
    expect(container.querySelectorAll("[data-testid='stream-tail']")).toHaveLength(1);
    rerender(<StreamingText text={"One\n\nTwo words"} streaming={false} />);
    expect(screen.queryByTestId("stream-tail")).toBeNull();
    expect(container.querySelector(".stream-tail")).toBeNull();
  });
});

describe("chip targets select the object", () => {
  it("/activity?run= is a typed search param that opens the run", () => {
    const validate = (
      ActivityRoute as unknown as {
        options: { validateSearch: (s: Record<string, unknown>) => unknown };
      }
    ).options.validateSearch;
    const id = "6f1c2b9e-3d4a-4c5b-9e8f-0a1b2c3d4e5f";
    expect(validate({ run: id })).toEqual({ run: id });
    expect(validate({ run: 4 })).toEqual({});
    expect(validate({ run: "r-9" })).toEqual({});
    expect(validate({ run: "../../members/x" })).toEqual({});
    wrap(<ActivityPage runId="r-9" />);
    const focus = within(screen.getByRole("region", { name: /Run/ }));
    expect(focus.getByText("Stock agent")).toBeInTheDocument();
    expect(focus.getByText("All counted")).toBeInTheDocument();
    expect(focus.getByText("done")).toBeInTheDocument();
  });

  it("/copilot?session= is a typed search param", () => {
    const validate = (
      CopilotRoute as unknown as {
        options: { validateSearch: (s: Record<string, unknown>) => unknown };
      }
    ).options.validateSearch;
    const id = "6f1c2b9e-3d4a-4c5b-9e8f-0a1b2c3d4e5f";
    expect(validate({ session: id })).toEqual({ session: id });
    expect(validate({})).toEqual({});
    expect(validate({ session: "../../members/x" })).toEqual({});
    expect(validate({ pin: `run:${id}` })).toEqual({ pin: `run:${id}` });
    // Kept as text: the page checks it (parsePin) before creating anything,
    // and tells the member when it is not a pin it can open.
    expect(validate({ pin: "run:../../members/x" })).toEqual({ pin: "run:../../members/x" });
    expect(validate({ pin: ["run"] })).toEqual({});
  });
});
