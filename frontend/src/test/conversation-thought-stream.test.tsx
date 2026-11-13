import { fireEvent, render, screen } from "@testing-library/react";
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

import { StreamingText } from "@/components/conversation/streaming-text";
import { ThoughtDisclosure, thoughtOf } from "@/components/conversation/thought-disclosure";

describe("ThoughtDisclosure", () => {
  it("is collapsed by default and opens on click", () => {
    render(<ThoughtDisclosure live={false} text="Check the ledger first." seconds={4} />);
    const toggle = screen.getByRole("button", { name: /Thought · 4 s/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Check the ledger first.")).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Check the ledger first.")).toBeInTheDocument();
  });

  it("leaves 'Thinking …' to the presence line and offers live reasoning as 'Thoughts'", () => {
    const { container, rerender } = render(<ThoughtDisclosure live text={null} seconds={null} />);

    expect(container).toBeEmptyDOMElement();

    rerender(<ThoughtDisclosure live text="Check the ledger." seconds={null} />);

    expect(screen.getByRole("button", { name: "Thoughts" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText(/Thinking/)).toBeNull();
    expect(document.querySelector(".text-shimmer")).toBeNull();
  });

  it("renders nothing for a finished turn without reasoning", () => {
    const { container } = render(<ThoughtDisclosure live={false} text={null} seconds={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("derives text and seconds from step timings", () => {
    expect(
      thoughtOf({
        stepTimings: [
          { step: 1, reasoning: "a", modelWaitMs: 1500, tokensOut: 1, maxTokens: 10 },
          { step: 2, reasoning: "b", modelWaitMs: 2600, tokensOut: 1, maxTokens: 10 },
        ],
      }),
    ).toEqual({ text: "a\n\nb", seconds: 4 });
  });

  it("counts only steps whose reasoning is shown, and appends live reasoning", () => {
    expect(
      thoughtOf({
        stepTimings: [
          { step: 1, reasoning: "a", modelWaitMs: 2000 },
          { step: 2, reasoning: "   ", modelWaitMs: 9000 },
          { step: 3, modelWaitMs: 9000, tokensOut: 1, maxTokens: 10 },
        ],
        liveReasoning: { step: 4, text: "c" },
      }),
    ).toEqual({ text: "a\n\nc", seconds: 2 });
  });
});

describe("StreamingText", () => {
  it("shows the breathing tail while streaming and removes it when done", () => {
    const { rerender } = render(<StreamingText text="Hello" streaming />);
    const region = screen.getByText("Hello").closest("[aria-live]");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveAttribute("aria-busy", "true");
    expect(screen.getByTestId("stream-tail")).toHaveClass("stream-tail");
    rerender(<StreamingText text="Hello world" streaming={false} />);
    expect(screen.queryByTestId("stream-tail")).toBeNull();
  });
});
