import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({
  exportChat: vi.fn(),
  sessions: [] as Array<Record<string, unknown>>,
}));

vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/hooks/use-mobile", () => ({ useIsNarrow: () => false, useIsMobile: () => false }));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useMay: () => () => true,
}));
vi.mock("@/lib/hooks-chat", async (orig) => ({
  ...(await orig<typeof import("@/lib/hooks-chat")>()),
  useChatSessions: () => ({ data: m.sessions, isLoading: false }),
  useRenameChatSession: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteChatSession: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  exportChat: m.exportChat,
  useChatProjects: () => ({ data: [] }),
  useSetSessionPinned: () => ({ mutate: vi.fn() }),
  useSetSessionProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn() }),
  useSaveProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}));

import { CopilotSidebar } from "@/components/conversation/copilot-sidebar";
import { SessionPicker } from "@/components/conversation/session-picker";
import { EditionCompositionProvider } from "@/edition/composition";

const session = {
  id: "s1",
  agentId: "x",
  title: "Q4 plan",
  createdAt: "",
  lastMessageAt: "2026-11-08T08:00:00Z",
  pinned: false,
  projectId: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  m.exportChat.mockResolvedValue(undefined);
  m.sessions = [session];
});

describe("export in the sidebar menu", () => {
  function mount() {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <EditionCompositionProvider extensions={[]}>
          <CopilotSidebar
            agentId="x"
            sessionId={null}
            onSelect={vi.fn()}
            collapsed={false}
            onToggle={vi.fn()}
          />
        </EditionCompositionProvider>
      </QueryClientProvider>,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "More: Q4 plan" }), { key: "Enter" });
  }

  it("exports the row's chat as Markdown or JSON", () => {
    mount();
    fireEvent.click(screen.getByRole("menuitem", { name: "Export as Markdown" }));
    expect(m.exportChat).toHaveBeenCalledWith("s1", "md");
    fireEvent.keyDown(screen.getByRole("button", { name: "More: Q4 plan" }), { key: "Enter" });
    fireEvent.click(screen.getByRole("menuitem", { name: "Export as JSON" }));
    expect(m.exportChat).toHaveBeenLastCalledWith("s1", "json");
  });

  it("says so when the export fails", async () => {
    m.exportChat.mockRejectedValue(new Error("nope"));
    mount();
    fireEvent.click(screen.getByRole("menuitem", { name: "Export as JSON" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/not exported/);
  });
});

describe("export in the header menu", () => {
  it("exports the current chat", () => {
    render(<SessionPicker sessions={[session] as never} sessionId="s1" onSelect={vi.fn()} />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Switch chat" }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Export as JSON" }));
    expect(m.exportChat).toHaveBeenCalledWith("s1", "json");
  });
});

describe("exportChat", () => {
  const realFetch = globalThis.fetch;
  const realCreate = URL.createObjectURL;
  const realRevoke = URL.revokeObjectURL;

  afterEach(() => {
    globalThis.fetch = realFetch;
    URL.createObjectURL = realCreate;
    URL.revokeObjectURL = realRevoke;
    vi.restoreAllMocks();
  });

  it("fetches with the token and saves the file under the server's name", async () => {
    window.localStorage.setItem("oc8-community-token", "tok");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("# Q4 plan", {
        headers: { "content-disposition": 'attachment; filename="q4-plan.md"' },
      }),
    );
    globalThis.fetch = fetchMock as never;
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
    let saved = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      saved = this.download;
    });

    const actual =
      await vi.importActual<typeof import("@/lib/conversation-api")>("@/lib/conversation-api");
    await actual.exportChat("s1", "md");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/chat\/sessions\/s1\/export\?format=md$/);
    expect((init as RequestInit).headers).toMatchObject({ authorization: "Bearer tok" });
    expect(saved).toBe("q4-plan.md");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock");
  });

  it("rejects when the server refuses", async () => {
    window.localStorage.setItem("oc8-community-token", "tok");
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("", { status: 404 })) as never;
    const actual =
      await vi.importActual<typeof import("@/lib/conversation-api")>("@/lib/conversation-api");
    await expect(actual.exportChat("s1", "json")).rejects.toMatchObject({ status: 404 });
  });
});
