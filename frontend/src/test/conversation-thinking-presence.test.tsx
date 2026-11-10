// The "waiting for an answer" state: a breathing Copilot mark and a status
// line that follows the run, then a soft breathing tail (no blinking caret)
// while the answer streams.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));

import { AssistantTurn, PRESENCE_MIN_MS } from "@/components/conversation/assistant-turn";
import { presenceLabel } from "@/components/conversation/presence-label";
import { StreamingText } from "@/components/conversation/streaming-text";
import { ThinkingPresence } from "@/components/conversation/thinking-presence";
import type { RunActivityDTO } from "@/lib/hooks-chat";
import { LanguageProvider } from "@/lib/i18n";

const RUN: RunActivityDTO = {
  id: "r1",
  agentId: "a1",
  state: "running",
  phase: null,
  output: null,
  steps: 0,
  toolCalls: [],
  taskId: null,
  question: null,
  renderedComponents: [],
};

function turn(run: RunActivityDTO, client = new QueryClient()) {
  return (
    <QueryClientProvider client={client}>
      <LanguageProvider>
        <AssistantTurn
          sessionId="s"
          turn={{ kind: "assistant", key: "k", replyToId: "u1", variants: [], liveRunId: run.id }}
          run={run}
        />
      </LanguageProvider>
    </QueryClientProvider>
  );
}

const en = { t: (e: string) => e, lang: "en" };
const de = { t: (_e: string, d?: string) => d ?? _e, lang: "de" };

function withCall(call: Record<string, unknown>) {
  return { state: "running", phase: null, toolCalls: [{ step: 1, ...call }] };
}

afterEach(() => {
  window.localStorage.removeItem("oc8-lang");
  vi.useRealTimers();
});

describe("ThinkingPresence in a turn", () => {
  it("shows before the first word and goes when text arrives", () => {
    const client = new QueryClient();
    const { rerender } = render(turn(RUN, client));

    const presence = screen.getByTestId("thinking-presence");
    expect(presence).toHaveAttribute("role", "status");
    expect(presence).toHaveAttribute("aria-live", "polite");
    expect(presence).toHaveTextContent("Thinking …");
    expect(screen.getByTestId("presence-mark")).toHaveAttribute("aria-hidden", "true");
    // No empty caret beside it: the markdown root draws nothing yet.
    expect(document.querySelector(".stream-fresh")).toBeNull();

    vi.useFakeTimers();
    rerender(turn({ ...RUN, liveAnswer: "Hello" }, client));
    // It stays its minimum (no flash), then the words take over.
    expect(screen.getByTestId("thinking-presence")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(PRESENCE_MIN_MS);
    });
    expect(screen.queryByTestId("thinking-presence")).toBeNull();
    expect(screen.getByTestId("stream-tail")).toBeInTheDocument();
  });

  it("is not shown while the run waits on a person, nor once it ended", () => {
    const { rerender } = render(turn({ ...RUN, state: "waiting_for_input" }));
    expect(screen.queryByTestId("thinking-presence")).toBeNull();
    rerender(turn({ ...RUN, state: "done", output: "" }));
    expect(screen.queryByTestId("thinking-presence")).toBeNull();
  });

  it("speaks for the workspace start with the same mark, not a box of its own", () => {
    window.localStorage.setItem("oc8-lang", "de");
    render(turn({ ...RUN, phase: "provisioning" }));
    const presence = screen.getByTestId("thinking-presence");
    expect(presence).toHaveTextContent("Arbeitsumgebung wird gestartet …");
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("follows the newest step: a page read names its host", () => {
    window.localStorage.setItem("oc8-lang", "de");
    render(
      turn({
        ...RUN,
        steps: 1,
        toolCalls: [
          { step: 1, tool: "fetch_url", arguments: { url: "https://www.opaas.cloud/preise" } },
        ],
      }),
    );
    expect(screen.getByTestId("thinking-presence")).toHaveTextContent("Liest www.opaas.cloud …");
  });

  it("names a delegate the client already knows", () => {
    const client = new QueryClient();
    client.setQueryData(["agents", "agent-7"], { id: "agent-7", name: "Mira" });
    render(
      turn(
        {
          ...RUN,
          toolCalls: [{ step: 1, tool: "delegate_task", arguments: { agent_id: "agent-7" } }],
        },
        client,
      ),
    );
    expect(screen.getByTestId("thinking-presence")).toHaveTextContent("Asking Mira …");
  });
});

describe("presenceLabel", () => {
  it("says thinking by default and before any step", () => {
    expect(presenceLabel(null, en).text).toBe("Thinking …");
    expect(presenceLabel({ state: "running", toolCalls: [] }, de).text).toBe("Denkt nach …");
  });

  it("reads a page by host, in both languages", () => {
    const run = withCall({ tool: "fetch_url", arguments: { url: "https://www.opaas.cloud/x" } });
    expect(presenceLabel(run, de).text).toBe("Liest www.opaas.cloud …");
    expect(presenceLabel(run, en).text).toBe("Reading www.opaas.cloud …");
    expect(presenceLabel(withCall({ tool: "fetch_url", arguments: {} }), de).text).toBe(
      "Liest eine Webseite …",
    );
  });

  it("searches the knowledge base and checks approvals in friendly words", () => {
    expect(presenceLabel(withCall({ tool: "search_knowledge" }), de).text).toBe(
      "Sucht im Wissen …",
    );
    expect(presenceLabel(withCall({ tool: "list_pending_approvals" }), de).text).toBe(
      "Prüft deine Freigaben …",
    );
    expect(
      presenceLabel(withCall({ tool: "delegate_task", arguments: { agent_id: "x" } }), de).text,
    ).toBe("Fragt einen Kollegen …");
  });

  it("uses a tool's own running label, else names the tool", () => {
    expect(presenceLabel(withCall({ tool: "todo_write" }), de).text).toBe(
      "Aktualisiert seinen Plan …",
    );
    expect(presenceLabel(withCall({ tool: "crm_lookup_deal" }), de).text).toBe(
      "Arbeitet an crm lookup deal …",
    );
    expect(presenceLabel(withCall({ tool: "crm_lookup_deal" }), en).text).toBe(
      "Working on crm lookup deal …",
    );
  });

  it("changes its key with every new step, so the line is re-announced", () => {
    const one = presenceLabel(withCall({ tool: "search_knowledge" }), en);
    const two = presenceLabel(
      {
        state: "running",
        toolCalls: [{ tool: "search_knowledge" }, { tool: "search_knowledge" }],
      },
      en,
    );
    expect(one.key).not.toBe(two.key);
  });
});

describe("the presence crossfade", () => {
  it("keeps the old words hidden from screen readers for one fade, then drops them", () => {
    vi.useFakeTimers();
    const client = new QueryClient();
    const view = (run: Parameters<typeof ThinkingPresence>[0]["run"]) => (
      <QueryClientProvider client={client}>
        <ThinkingPresence run={run} />
      </QueryClientProvider>
    );
    const { rerender } = render(view({ state: "running", toolCalls: [] }));
    rerender(view(withCall({ tool: "search_knowledge" })));

    const outgoing = screen.getByText("Thinking …");
    expect(outgoing).toHaveAttribute("aria-hidden", "true");
    expect(outgoing).toHaveClass("presence-out", "motion-reduce:hidden");
    expect(screen.getByText("Searching the knowledge base …")).toHaveClass("presence-in");

    act(() => {
      vi.advanceTimersByTime(250);
    });
    expect(screen.queryByText("Thinking …")).toBeNull();
  });
});

describe("motion", () => {
  const css = readFileSync(join(process.cwd(), "src", "styles.css"), "utf8");
  const block = (name: string) => {
    const start = css.indexOf(`@utility ${name} {`);
    const next = css.indexOf("\n@", start + 1);
    return css.slice(start, next === -1 ? undefined : next);
  };

  it("stills the mark, the words and the tail under reduced motion", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ThinkingPresence run={null} />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("presence-mark")).toHaveClass(
      "presence-mark",
      "motion-reduce:animate-none",
    );
    for (const name of ["presence-mark", "presence-in", "stream-tail"]) {
      expect(block(name)).toContain("@media (prefers-reduced-motion: reduce)");
    }
  });

  it("breathes the mark on a 1.6 s ease-in-out loop between 0.9 and 1.05", () => {
    expect(block("presence-mark")).toContain("1.6s ease-in-out infinite");
    expect(css).toMatch(/@keyframes oc8-breathe[\s\S]*scale\(0\.9\)[\s\S]*scale\(1\.05\)/);
  });
});

describe("the streaming tail", () => {
  it("replaces the blinking caret", () => {
    const css = readFileSync(join(process.cwd(), "src", "styles.css"), "utf8");
    expect(css).not.toContain("@utility stream-caret");
    expect(css).not.toContain("oc8-caret-blink");
    expect(css).not.toContain("step-end infinite");
  });

  it("fades the newest words in while streaming and leaves plain text when done", () => {
    const { container, rerender } = render(<StreamingText text="Two words" streaming />);
    expect(container.querySelector(".stream-fresh")).toHaveTextContent("Two words");
    expect(screen.getByTestId("stream-tail")).toHaveClass("stream-tail");
    const paragraph = container.querySelector("p");

    rerender(<StreamingText text={"Two words and more"} streaming={false} />);
    // The paragraph keeps its node; nothing is left to fade.
    expect(container.querySelector("p")).toBe(paragraph);
    expect(container.querySelector(".stream-fresh")).toBeNull();
    expect(container.querySelector(".stream-tail")).toBeNull();
    expect(screen.getByText("Two words and more")).toBeInTheDocument();
  });

  it("leaves code untouched", () => {
    const { container } = render(<StreamingText text={"Run `npm ci` now"} streaming />);
    expect(container.querySelector("code")?.textContent).toBe("npm ci");
    expect(container.querySelector("code .stream-fresh")).toBeNull();
  });
});
