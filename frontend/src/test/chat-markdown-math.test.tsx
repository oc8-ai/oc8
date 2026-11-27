// Answers render LaTeX: `$…$`, `$$…$$`, `\(…\)` and `\[…\]` become KaTeX, but
// a price ("$5 and $6") stays text, and while an answer streams an unclosed
// math span stays text instead of flickering through half-typed formulas.
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/record-links", async (orig) => ({
  ...(await orig<typeof import("@/lib/record-links")>()),
  useVerifiedLinks: () => ({ data: undefined }),
}));

import { ChatMarkdown } from "@/components/chat-markdown";
import { splitBlocks, stabilizeTail } from "@/components/markdown-stream";

/** Waits until KaTeX (its own chunk) has rendered `count` formulas. */
async function katexNodes(container: HTMLElement, count = 1) {
  await screen.findAllByText((_, el) => !!el?.classList.contains("katex"));
  const nodes = container.querySelectorAll(".katex");
  expect(nodes.length).toBe(count);
  return nodes;
}

/** Nothing parsed as math at all -- no KaTeX now, and none on its way. */
function noMath(container: HTMLElement) {
  expect(container.querySelector(".katex")).toBeNull();
  expect(container.querySelector(".math-inline, .math-display")).toBeNull();
}

describe("ChatMarkdown math", () => {
  it("renders inline $…$ as KaTeX", async () => {
    const { container } = render(<ChatMarkdown text="Energy: $E=mc^2$ holds." />);
    await katexNodes(container);
    expect(container.querySelector(".katex-display")).toBeNull();
    expect(container.textContent).toContain("Energy:");
  });

  it("renders a $$ block as display math", async () => {
    const { container } = render(<ChatMarkdown text={"Area:\n\n$$\n\\pi r^2\n$$\n\nDone."} />);
    await katexNodes(container);
    expect(container.querySelector(".katex-display")).not.toBeNull();
  });

  it("renders \\(…\\) inline and \\[…\\] as display math", async () => {
    const { container } = render(
      <ChatMarkdown text={"Inline \\(a+b\\) here.\n\n\\[\n\\frac{1}{2}\n\\]"} />,
    );
    await katexNodes(container, 2);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(1);
  });

  it("keeps dollar amounts as text", () => {
    const { container } = render(
      <ChatMarkdown text={"It costs $5 and $6. A range: $5-$10, a ratio $5/$6, and $ 7 $."} />,
    );
    noMath(container);
    expect(container.textContent).toContain("It costs $5 and $6.");
    expect(container.textContent).toContain("$5-$10");
  });

  it("finds the formula after a price on the same line", async () => {
    const { container } = render(<ChatMarkdown text="For $5 you get $x^2$." />);
    await katexNodes(container);
    expect(container.textContent).toContain("For $5 you get");
  });

  it("does not run a formula from a price to the next formula", async () => {
    const { container } = render(<ChatMarkdown text="Costs US$5 and **$x$** more." />);
    await katexNodes(container);
    expect(container.textContent).toContain("Costs US$5 and");
    expect(container.querySelector("strong .katex")).not.toBeNull();
  });

  it("keeps math in code as code", () => {
    const { container } = render(<ChatMarkdown text={"Use `$x$` or\n\n```\n$$y$$\n```"} />);
    noMath(container);
  });

  it("keeps an escaped dollar as text", () => {
    const { container } = render(<ChatMarkdown text={"\\$x$ stays"} />);
    noMath(container);
    expect(container.textContent).toContain("$x$ stays");
  });

  it("does not turn [n] inside a formula into a citation", async () => {
    const { container } = render(
      <ChatMarkdown text="See $a[1]$ and [1]." sources={[{ n: 1, title: "Doc" } as never]} />,
    );
    await katexNodes(container);
    expect(screen.getAllByRole("button", { name: /Source 1/ })).toHaveLength(1);
  });

  it("keeps an unclosed $… as text while streaming", () => {
    const { container } = render(<ChatMarkdown text="so $x_1 + y" streaming />);
    noMath(container);
    expect(container.textContent).toContain("so $x_1 + y");
  });

  it("shows an unclosed $$ block as text while streaming", () => {
    const { container } = render(<ChatMarkdown text={"Area:\n\n$$\n\\frac{a_1}{b_1"} streaming />);
    noMath(container);
    expect(container.textContent).toContain("\\frac{a_1}{b_1");
  });

  it("renders the formula once its closing $$ arrives", async () => {
    const { container, rerender } = render(
      <ChatMarkdown text={"Area:\n\n$$\n\\pi r^2"} streaming />,
    );
    noMath(container);
    rerender(<ChatMarkdown text={"Area:\n\n$$\n\\pi r^2\n$$"} streaming />);
    await katexNodes(container);
  });
});

describe("markdown-stream and math", () => {
  it("never splits a $$ block at a blank line inside it", () => {
    const text = "Intro\n\n$$\na\n\nb\n$$\n\nAfter";
    expect(splitBlocks(text)).toEqual(["Intro\n\n", "$$\na\n\nb\n$$\n\n", "After"]);
  });

  it("does not take a one-line $$x$$ for an opening fence", () => {
    expect(splitBlocks("$$x$$\n\nNext")).toEqual(["$$x$$\n\n", "Next"]);
  });

  it("leaves the inside of an open $$ block alone", () => {
    // `**` inside TeX is no emphasis to close.
    expect(stabilizeTail("$$\na**b")).not.toContain("a**b**");
  });
});
