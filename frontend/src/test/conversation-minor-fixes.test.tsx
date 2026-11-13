// frontend/src/test/conversation-minor-fixes.test.tsx
// Small things from the final review: long words wrap at 375 px, the empty
// Copilot chat looks the same on every surface, a failed rating is said.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

// ChatMarkdown's record-link check, without a server: in a saved answer no
// link points at a connected system, so each is an ordinary one.
vi.mock("@/lib/record-links", () => ({
  useVerifiedLinks: (messageId: string | undefined, urls: string[]) => ({
    data: messageId
      ? new Map(urls.map((url) => [url, { url, connected: false, issued: false }]))
      : undefined,
  }),
}));

const feedback = vi.hoisted(() => vi.fn());
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useMessageFeedback: () => ({ mutate: feedback }),
  useTurnRun: () => ({ data: undefined }),
}));

import { ChatMarkdown } from "@/components/chat-markdown";
import { AssistantTurn } from "@/components/conversation/assistant-turn";

describe("long words and URLs", () => {
  it("wrap inside the answer instead of widening the column", () => {
    const url = `https://example.com/${"a".repeat(200)}`;
    const { container } = render(<ChatMarkdown text={`See [${url}](${url})`} messageId="m1" />);
    expect((container.firstElementChild as HTMLElement).className).toContain(
      "[overflow-wrap:anywhere]",
    );
    const link = screen.getByRole("link");
    expect(link.className).toMatch(/(^|\s)inline(\s|$)/);
    expect(link.className).not.toContain("inline-flex");
  });
});

describe("the empty Copilot chat", () => {
  it.each([
    "routes/copilot.tsx",
    "components/copilot-dock.tsx",
    "components/dashboard/widgets/chat-widget.tsx",
  ])("%s offers the same starters and an empty state", (rel) => {
    const source = readFileSync(join(process.cwd(), "src", rel), "utf8");
    expect(source).toMatch(/copilotStarters\(t\)/);
    expect(source).toMatch(/emptyState=/);
  });
});

describe("a rating that was not saved", () => {
  it("reverts and says so", () => {
    feedback.mockImplementation((_v, opts: { onError: () => void }) => opts.onError());
    const message = {
      id: "a1",
      sessionId: "s",
      role: "assistant" as const,
      content: "Done.",
      runId: null,
      renderedComponents: [],
      createdAt: "",
      attachments: [],
      mode: null,
      contextRefs: [],
      authorMemberId: null,
      authorName: null,
      replyToId: "u1",
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AssistantTurn
          sessionId="s"
          turn={{
            kind: "assistant",
            key: "a",
            replyToId: "u1",
            variants: [message],
            liveRunId: null,
          }}
        />
      </QueryClientProvider>,
    );
    const good = screen.getByRole("button", { name: "Good answer" });
    fireEvent.click(good);
    expect(good).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("alert")).toHaveTextContent("Your rating was not saved.");
  });
});
