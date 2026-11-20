import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({
  post: vi.fn(),
  put: vi.fn(),
  may: true,
  linkedError: null as unknown,
}));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  api: {
    get: vi.fn().mockResolvedValue({ agentId: "assistant" }),
    post: m.post,
    put: m.put,
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useMay: () => () => m.may,
}));
vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (opts: unknown) => opts,
  useRouter: () => null,
  useNavigate: () => vi.fn(),
  useRouterState: () => "k",
}));
vi.mock("@/lib/agent-builder-api", () => ({ useAgentIdeas: () => ({ data: [] }) }));
vi.mock("@/hooks/use-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/conversation-api", () => ({
  useChatSession: () => ({ data: undefined, error: m.linkedError }),
}));
vi.mock("@/lib/hooks", () => ({
  useAgent: () => ({ data: undefined }),
  useAuth: () => ({ data: { memberId: "me" }, isError: false }),
}));
vi.mock("@/lib/copilot-work-count", () => ({ useCopilotWorkCount: () => 0 }));
vi.mock("@/lib/hooks-chat", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks-chat")>()),
  useChatSessions: () => ({ data: [] }),
}));
vi.mock("@/lib/hooks-copilot", () => ({
  usePauseCopilot: () => ({ mutate: vi.fn(), isPending: false }),
  useResumeCopilot: () => ({ mutate: vi.fn(), isPending: false }),
  useCopilotProfile: () => ({
    data: {
      displayName: "Pip",
      avatar: { shape: "round", color: "indigo" },
      status: "ready",
      activeCount: 0,
      agentId: "assistant",
    },
  }),
}));
vi.mock("@/components/conversation", () => ({
  Conversation: ({ sessionId }: { sessionId: string | null }) => (
    <div data-testid="chat" data-session={sessionId ?? ""} />
  ),
}));
vi.mock("@/components/conversation/session-picker", () => ({ SessionPicker: () => null }));
vi.mock("@/components/conversation/copilot-sidebar", () => ({ CopilotSidebar: () => null }));
vi.mock("@/components/copilot-work-panel", () => ({ CopilotWorkPanel: () => null }));

import { ApiError } from "@/lib/api";
import { DiscussWithCopilot } from "@/components/conversation/discuss-with-copilot";
import {
  discussHref,
  parsePin,
  useOpenPinnedChat,
} from "@/components/conversation/open-pinned-chat";
import { CopilotPage, Route } from "@/routes/copilot";

const UUID = "11111111-2222-3333-4444-555555555555";

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  m.post.mockReset();
  m.put.mockReset();
  m.may = true;
  m.linkedError = null;
});

describe("Discuss with Copilot", () => {
  it("links to the copilot with the object pinned", () => {
    render(<DiscussWithCopilot kind="approval" id="ap1" label="Refund 40 €" />, { wrapper });
    const link = screen.getByRole("link", { name: "Discuss with Copilot: Refund 40 €" });
    expect(link).toHaveAttribute("href", discussHref("approval", "ap1"));
    expect(link.getAttribute("href")).toBe("/copilot?pin=approval%3Aap1");
  });

  it("is hidden without copilot:use", () => {
    m.may = false;
    render(<DiscussWithCopilot kind="agent" id="a" label="A" />, { wrapper });
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("creates an assistant chat and pins the object", async () => {
    m.post.mockResolvedValue({ id: "s1", agentId: "assistant" });
    m.put.mockResolvedValue({ id: "s1", pinnedRefs: [] });
    const { result } = renderHook(() => useOpenPinnedChat(), { wrapper });
    const opened = await result.current({ kind: "run", id: UUID });
    expect(opened).toEqual({ sessionId: "s1", pinned: "ok" });
    expect(m.post).toHaveBeenCalledWith("/chat/sessions", { agentId: "assistant" });
    expect(m.put).toHaveBeenCalledWith("/chat/sessions/s1/pins", {
      refs: [{ kind: "run", id: UUID }],
    });
  });

  it("keeps the new chat when the pin is refused", async () => {
    m.post.mockResolvedValue({ id: "s1", agentId: "assistant" });
    m.put.mockRejectedValue(new ApiError("{}", 422));
    const { result } = renderHook(() => useOpenPinnedChat(), { wrapper });
    expect(await result.current({ kind: "run", id: UUID })).toEqual({
      sessionId: "s1",
      pinned: "refused",
    });
  });
});

describe("parsePin", () => {
  it("accepts a known kind with a uuid", () => {
    expect(parsePin(`approval:${UUID}`)).toEqual({ kind: "approval", id: UUID });
  });
  it.each([
    ["nothing", undefined],
    ["no colon", "run"],
    ["unknown kind", `evil:${UUID}`],
    ["not a uuid", "run:../../members/x"],
    ["extra segment", `run:${UUID}:x`],
    ["empty id", "run:"],
    ["source_file without file", `source_file:${UUID}`],
    ["source_file with empty file", `source_file:${UUID}:`],
    ["source_file with control chars", `source_file:${UUID}:a%00b`],
    ["source_file with bad escape", `source_file:${UUID}:%E0%A4%A`],
    ["source_file too long", `source_file:${UUID}:${"a".repeat(2049)}`],
    ["source_file with bad source", "source_file:abc:file"],
  ])("rejects %s", (_n, raw) => {
    expect(parsePin(raw)).toBeNull();
  });
  it("decodes the file part of a source_file", () => {
    expect(parsePin(`source_file:${UUID}:docs%2Fa%20b.pdf`)).toEqual({
      kind: "source_file",
      id: `${UUID}:docs/a b.pdf`,
    });
  });
});

describe("/copilot?pin=", () => {
  const page = (pin: string | undefined, onPinHandled = vi.fn(), navKey = "k1") => (
    <StrictMode>
      <QueryClientProvider client={new QueryClient()}>
        <CopilotPage pin={pin} navKey={navKey} onPinHandled={onPinHandled} />
      </QueryClientProvider>
    </StrictMode>
  );

  it("creates one chat under StrictMode, pins, selects it and drops the pin from the URL, sending nothing", async () => {
    m.post.mockResolvedValue({ id: "s1", agentId: "assistant" });
    m.put.mockResolvedValue({ id: "s1", pinnedRefs: [] });
    const done = vi.fn();
    render(page(`run:${UUID}`, done));
    await waitFor(() => expect(done).toHaveBeenCalledWith("s1"));
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "s1");
    expect(m.post).toHaveBeenCalledTimes(1);
    expect(m.post).toHaveBeenCalledWith("/chat/sessions", { agentId: "assistant" });
    expect(m.put).toHaveBeenCalledTimes(1);
    expect(done).toHaveBeenCalledTimes(1);
  });

  it("does not create a second chat when the same link is followed again while the first is in flight", async () => {
    let release: (v: unknown) => void = () => undefined;
    m.post.mockReturnValue(new Promise((r) => (release = r)));
    m.put.mockResolvedValue({});
    const done = vi.fn();
    const ui = (key: string) => page(`run:${UUID}`, done, key);
    const { rerender } = render(ui("k1"));
    rerender(ui("k2"));
    await act(async () => release({ id: "s1", agentId: "assistant" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(m.post).toHaveBeenCalledTimes(1);
  });

  it("ignores an invalid pin, opens a plain chat with a note and creates nothing", () => {
    const done = vi.fn();
    render(page("run:../../members/x", done));
    expect(m.post).not.toHaveBeenCalled();
    expect(done).toHaveBeenCalledWith(null);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "");
  });

  it("keeps the chat and says so when the object is not visible to the member", async () => {
    m.post.mockResolvedValue({ id: "s1", agentId: "assistant" });
    m.put.mockRejectedValue(new ApiError('{"refused":[]}', 422));
    const done = vi.fn();
    render(page(`approval:${UUID}`, done));
    await waitFor(() => expect(done).toHaveBeenCalledWith("s1"));
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "s1");
    expect(screen.getByRole("status")).toHaveTextContent(/can't pin that|kannst du nicht anheften/);
  });

  it("does not select, note or navigate when the reply arrives after the page is gone", async () => {
    let release: (v: unknown) => void = () => undefined;
    m.post.mockReturnValue(new Promise((r) => (release = r)));
    m.put.mockResolvedValue({});
    const done = vi.fn();
    const { unmount } = render(page(`run:${UUID}`, done));
    await waitFor(() => expect(m.post).toHaveBeenCalled());
    unmount();
    await act(async () => release({ id: "s1", agentId: "assistant" }));
    expect(done).not.toHaveBeenCalled();
  });

  it("says so when ?session= names a chat that is gone or not the member's", () => {
    m.linkedError = new ApiError("not found", 404);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <CopilotPage initialSessionId="s-x" />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/gehört nicht dir|isn't yours/);
    expect(screen.getByTestId("chat")).toHaveAttribute("data-session", "");
  });

  it("does nothing for a member without copilot:use", () => {
    m.may = false;
    render(page(`run:${UUID}`));
    expect(m.post).not.toHaveBeenCalled();
  });
});

describe("/copilot search validation", () => {
  const validate = (Route as unknown as { validateSearch: (s: Record<string, unknown>) => unknown })
    .validateSearch;
  it("keeps a pin of at most 4200 characters and drops a longer one", () => {
    const ok = `run:${"a".repeat(4196)}`;
    expect(validate({ pin: ok })).toEqual({ pin: ok });
    expect(validate({ pin: `${ok}a` })).toEqual({});
  });
});
