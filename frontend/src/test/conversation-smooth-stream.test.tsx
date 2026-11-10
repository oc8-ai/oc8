// A streamed answer is revealed at a steady, catching-up pace instead of in
// the bursts its fragments arrive in: word by word, never more than about a
// second behind, all at once when the run ends or under reduced motion. The
// blocks already on screen keep their nodes, only the newest words fade, and
// unfinished markdown renders the way it will once finished.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));

import { AssistantTurn, PRESENCE_MIN_MS } from "@/components/conversation/assistant-turn";
import { BASE_RATE, revealStep, wordBoundary } from "@/components/conversation/smooth-reveal";
import { StreamingText } from "@/components/conversation/streaming-text";
import { splitBlocks, stabilizeTail } from "@/components/markdown-stream";
import { LanguageProvider } from "@/lib/i18n";
import type { RunActivityDTO } from "@/lib/hooks-chat";
import { mergeTurnRun } from "@/lib/conversation-api";
import { applyEvent, runLiveAnswerKey } from "@/lib/live/apply-event";

const FAKE = {
  toFake: [
    "setTimeout",
    "clearTimeout",
    "requestAnimationFrame",
    "cancelAnimationFrame",
    "performance",
    "Date",
  ] as const,
};

function shownText(container: HTMLElement): string {
  return (container.querySelector("[data-testid='stream-tail']")?.textContent ?? "").trim();
}

function frames(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

const BURST = "The quick brown fox jumps over the lazy dog and keeps on running far away. ".repeat(
  3,
);

describe("pacing (pure)", () => {
  it("reveals at the base rate with little waiting, faster with more", () => {
    expect(revealStep(0, 1 / 60)).toBe(0);
    expect(revealStep(10, 1)).toBe(10);
    const small = revealStep(20, 0.1);
    const big = revealStep(2000, 0.1);
    expect(small).toBeGreaterThanOrEqual(BASE_RATE * 0.1);
    expect(big).toBeGreaterThan(small * 10);
  });

  it("never runs more than about a second behind a bursty stream", () => {
    // 400 characters a second, arriving as 200-character bursts twice a second.
    const rate = 400;
    let received = 0;
    let cursor = 0;
    let worst = 0;
    for (let frame = 0; frame < 60 * 10; frame += 1) {
      if (frame % 30 === 0) received += 200;
      cursor += revealStep(received - cursor, 1 / 60);
      worst = Math.max(worst, (received - cursor) / rate);
    }
    expect(worst).toBeLessThan(1);
    // And once the bursts stop, it catches up.
    for (let frame = 0; frame < 120; frame += 1) {
      cursor += revealStep(received - cursor, 1 / 60);
    }
    expect(received - cursor).toBeLessThan(1);
  });

  it("cuts between words, never inside one", () => {
    const text = "Grüße aus Köln, schönen Tag";
    for (let pos = 1; pos <= text.length; pos += 1) {
      const cut = wordBoundary(text, 0, pos, true);
      expect(cut).toBeLessThanOrEqual(pos);
      // At a cut, the word before it is whole.
      if (cut > 0 && cut < text.length) {
        expect(/[\p{L}]/u.test(text[cut - 1]) && /[\p{L}]/u.test(text[cut])).toBe(false);
      }
    }
    // The end of a text still streaming may be half a word...
    expect(wordBoundary("Hello wor", 0, 9, true)).toBe(6);
    // ...unless the stream has ended or the text ends in a space.
    expect(wordBoundary("Hello wor", 0, 9, false)).toBe(9);
    expect(wordBoundary("Hello world ", 0, 12, true)).toBe(12);
  });
});

describe("StreamingText reveal", () => {
  beforeEach(() => {
    vi.useFakeTimers(FAKE);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reveals a burst progressively, word by word, and catches up", () => {
    const { container, rerender } = render(<StreamingText text="" streaming />);
    rerender(<StreamingText text={BURST} streaming />);
    expect(shownText(container)).toBe("");

    frames(100);
    const partial = shownText(container);
    expect(partial.length).toBeGreaterThan(0);
    expect(partial.length).toBeLessThan(BURST.trim().length);
    expect(BURST.startsWith(partial)).toBe(true);
    // Cut at the end of a word.
    expect(BURST[partial.length]).toMatch(/[\s.]/);

    frames(150);
    expect(shownText(container).length).toBeGreaterThan(partial.length);

    frames(1500);
    expect(shownText(container)).toBe(BURST.trim());
  });

  it("shows everything at once when the run finishes", () => {
    const { container, rerender } = render(<StreamingText text="" streaming />);
    rerender(<StreamingText text={BURST} streaming />);
    frames(50);
    expect(shownText(container).length).toBeLessThan(BURST.trim().length);
    rerender(<StreamingText text={BURST} streaming={false} />);
    expect(container.textContent?.trim()).toBe(BURST.trim());
    expect(container.querySelector(".stream-tail")).toBeNull();
  });

  it("keeps finished blocks mounted and fades only the newest words", () => {
    const first = "First paragraph, done.\n\n";
    const { container, rerender } = render(
      <StreamingText text={`${first}Second one `} streaming />,
    );
    const [p1, p2] = [...container.querySelectorAll("p")];
    expect(p1).toHaveTextContent("First paragraph, done.");

    rerender(<StreamingText text={`${first}Second one is growing slowly. `} streaming />);
    frames(2000);
    const after = [...container.querySelectorAll("p")];
    // Same nodes: nothing re-mounted, nothing faded again.
    expect(after[0]).toBe(p1);
    expect(after[1]).toBe(p2);
    expect(p2).toHaveTextContent("Second one is growing slowly.");
    // The finished block has no fresh words; the last block at most one span.
    expect(p1.querySelector(".stream-fresh")).toBeNull();
    expect(container.querySelectorAll(".stream-fresh").length).toBeLessThanOrEqual(1);
    const fresh = container.querySelector(".stream-fresh");
    if (fresh) expect("Second one is growing slowly.").toContain(fresh.textContent?.trim() ?? "");

    rerender(<StreamingText text={`${first}Second one is growing slowly. `} streaming={false} />);
    expect(container.querySelector(".stream-fresh")).toBeNull();
    expect(container.querySelectorAll("p")[0]).toBe(p1);
  });

  it("fades a newly revealed word in its own span, keyed so the fade plays once", () => {
    const { container, rerender } = render(<StreamingText text="Hello " streaming />);
    rerender(<StreamingText text="Hello there " streaming />);
    frames(500);
    const fresh = container.querySelector(".stream-fresh");
    expect(fresh?.textContent).toBe("there");
    rerender(<StreamingText text="Hello there friend " streaming />);
    frames(500);
    const next = container.querySelector(".stream-fresh");
    expect(next?.textContent).toBe("friend");
    expect(next).not.toBe(fresh);
    // The word before is plain text now, not a span of its own.
    expect(container.querySelector("p")?.childNodes[0].textContent).toBe("Hello there ");
  });

  it("renders an unclosed bold as bold, not as asterisks", () => {
    const { container } = render(<StreamingText text="This is **really impor" streaming />);
    expect(container.querySelector("strong")).toHaveTextContent("really");
    expect(container.textContent).not.toContain("**");
  });

  it("reveals at once under reduced motion", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("reduce"),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    const { container, rerender } = render(<StreamingText text="" streaming />);
    rerender(<StreamingText text={BURST} streaming />);
    expect(shownText(container)).toBe(BURST.trim());
  });

  it("keeps code untouched while streaming", () => {
    const { container } = render(<StreamingText text={"Run `npm ci` now"} streaming />);
    expect(container.querySelector("code")?.textContent).toBe("npm ci");
    expect(container.querySelector("code .stream-fresh")).toBeNull();
  });
});

describe("unfinished markdown", () => {
  it("closes bold, strike and inline code early, or drops an empty marker", () => {
    expect(stabilizeTail("Say **hel")).toBe("Say **hel**");
    expect(stabilizeTail("Say **hello ")).toBe("Say **hello**");
    expect(stabilizeTail("Say ~~no")).toBe("Say ~~no~~");
    expect(stabilizeTail("Run `npm")).toBe("Run `npm`");
    expect(stabilizeTail("Then **")).toBe("Then ");
    expect(stabilizeTail("**a** and **b")).toBe("**a** and **b**");
    // A ** inside code is code.
    expect(stabilizeTail("Use `a**b")).toBe("Use `a**b`");
  });

  it("shows a half-typed link as its words", () => {
    expect(stabilizeTail("See [the docs](https://exa")).toBe("See the docs");
    expect(stabilizeTail("See [the do")).toBe("See the do");
    expect(stabilizeTail("See [the docs](https://example.com) now")).toBe(
      "See [the docs](https://example.com) now",
    );
  });

  it("holds table rows until the header rule, and a bare marker line", () => {
    expect(stabilizeTail("Intro\n\n| a | b |")).toBe("Intro\n");
    expect(stabilizeTail("| a | b |\n| - | - |\n| 1 | 2")).toBe("| a | b |\n| - | - |\n| 1 | 2");
    // A rule still being typed is not one yet.
    expect(stabilizeTail("Intro\n\n| a | b |\n| --")).toBe("Intro\n");
    expect(stabilizeTail("Title\n--")).toBe("Title");
    expect(stabilizeTail("Items:\n-")).toBe("Items:");
  });

  it("leaves an open code fence to grow as a code block", () => {
    const open = "Here:\n\n```ts\nconst a = **b";
    expect(stabilizeTail(open)).toBe(open);
  });

  it("does not break the layout mid-stream", () => {
    const { container } = render(
      <StreamingText text={"Intro\n\n| a | b |\n| - | - |\n| 1 | **tw"} streaming />,
    );
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelector("td strong")).toHaveTextContent("tw");
    const fence = render(<StreamingText text={"Code:\n\n```\nlet x = 1"} streaming />);
    expect(fence.container.querySelector("pre")).toHaveTextContent("let x = 1");
  });
});

describe("splitBlocks", () => {
  it("cuts at blank lines and joins back to the text", () => {
    const text = "One\n\nTwo\nstill two\n\n\n# Three\n\n- a\n\n- b\n\n```\nx\n\ny\n```\n\nEnd";
    const blocks = splitBlocks(text);
    expect(blocks.join("")).toBe(text);
    expect(blocks[0]).toBe("One\n\n");
    expect(blocks[1]).toBe("Two\nstill two\n\n\n");
    // A loose list stays one block, and so does a fence with blank lines.
    expect(blocks.some((b) => b.includes("- a\n\n- b"))).toBe(true);
    expect(blocks.some((b) => b.includes("```\nx\n\ny\n```"))).toBe(true);
    expect(blocks[blocks.length - 1]).toBe("End");
  });

  it("keeps a text with reference definitions whole", () => {
    const text = "See [x].\n\n[x]: https://example.com";
    expect(splitBlocks(text)).toEqual([text]);
  });
});

describe("the waiting mark before the run is known", () => {
  afterEach(() => vi.useRealTimers());

  function pendingTurn(client: QueryClient, run?: RunActivityDTO) {
    return (
      <QueryClientProvider client={client}>
        <LanguageProvider>
          <AssistantTurn
            sessionId="s"
            turn={
              run
                ? { kind: "assistant", key: "k", replyToId: "u1", variants: [], liveRunId: run.id }
                : {
                    kind: "assistant",
                    key: "k",
                    replyToId: null,
                    variants: [],
                    liveRunId: null,
                    pending: true,
                  }
            }
            run={run}
          />
        </LanguageProvider>
      </QueryClientProvider>
    );
  }

  it("shows on a sent message with no run yet, and stays its minimum", () => {
    vi.useFakeTimers(FAKE);
    const client = new QueryClient();
    const { rerender } = render(pendingTurn(client));
    const mark = screen.getByTestId("presence-mark");
    expect(screen.getByTestId("thinking-presence")).toHaveTextContent("Thinking …");

    const run: RunActivityDTO = {
      id: "r1",
      agentId: "a",
      state: "running",
      phase: null,
      output: null,
      steps: 0,
      toolCalls: [],
      taskId: null,
      question: null,
      renderedComponents: [],
      liveAnswer: "Hi ",
    };
    rerender(pendingTurn(client, run));
    // The first word came at once: the mark stays, the same node.
    expect(screen.getByTestId("presence-mark")).toBe(mark);
    frames(PRESENCE_MIN_MS - 50);
    expect(screen.getByTestId("thinking-presence")).toBeInTheDocument();
    frames(100);
    expect(screen.queryByTestId("thinking-presence")).toBeNull();
    frames(500);
    expect(screen.getByTestId("stream-tail")).toHaveTextContent("Hi");
  });
});

describe("fragments that arrive before the run is cached", () => {
  it("are kept and laid over the first fetch", () => {
    const qc = new QueryClient();
    const delta = (text: string) => ({
      id: text,
      type: "run.token_delta",
      tenantid: "t",
      time: "",
      source: "",
      data: { run_id: "r1", text },
    });
    applyEvent(qc, delta("Hello "));
    applyEvent(qc, delta("world"));
    expect(qc.getQueryData(runLiveAnswerKey("r1"))).toBe("Hello world");
    const fresh = {
      id: "r1",
      agentId: "a",
      state: "queued",
      phase: null,
      output: null,
      steps: 0,
      toolCalls: [],
      taskId: null,
      question: null,
      renderedComponents: [],
    } satisfies RunActivityDTO;
    const merged = mergeTurnRun(fresh, undefined, "Hello world", {
      state: "running",
      phase: null,
    });
    expect(merged.liveAnswer).toBe("Hello world");
    expect(merged.state).toBe("running");
    // An ended run is taken as fetched: its output is complete.
    const done = mergeTurnRun(
      { ...fresh, state: "done", output: "Hi" },
      undefined,
      "Hel",
      undefined,
    );
    expect(done.liveAnswer).toBeUndefined();
  });
});
