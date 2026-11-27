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

// LLMs often put TeX on the `$$` lines themselves.
const ALIGNED = "$$\\begin{aligned}\n a &= b \\\\\n c &= d\n\\end{aligned}$$";

/** One display formula holding the aligned environment, and the words after
 *  it as prose outside any formula. */
async function alignedThenProse(container: HTMLElement) {
  await katexNodes(container);
  const display = container.querySelector(".katex-display");
  expect(display?.textContent).toContain("aligned");
  expect(display?.textContent).not.toContain("More text");
  const prose = [...container.querySelectorAll("p")].find((p) =>
    p.textContent?.includes("More text here."),
  );
  expect(prose).toBeDefined();
  expect(prose?.closest(".katex, pre")).toBeNull();
}

describe("display math with TeX on its $$ lines", () => {
  it.each([
    ["a blank line", "\n\n"],
    ["no blank line", "\n"],
  ])("ends at its closing $$ (%s after it)", async (_, gap) => {
    const { container } = render(<ChatMarkdown text={ALIGNED + gap + "More text here."} />);
    await alignedThenProse(container);
  });

  it("never swallows what follows while it streams", async () => {
    const text = "Solve:\n\n" + ALIGNED + "\n\nMore text here.";
    const { container, rerender } = render(<ChatMarkdown text="" streaming />);
    for (let k = 1; k <= text.length; k += 1) {
      rerender(<ChatMarkdown text={text.slice(0, k)} streaming />);
      for (const formula of container.querySelectorAll(".katex, .math-display, .math-inline")) {
        expect(formula.textContent).not.toContain("More");
      }
    }
    await alignedThenProse(container);
    rerender(<ChatMarkdown text={text} />);
    await alignedThenProse(container);
  });

  it("shows the formula as math source, not a code block, while open", () => {
    const { container } = render(<ChatMarkdown text={"$$\\begin{aligned}\n a &= b"} streaming />);
    noMath(container);
    expect(container.querySelector("pre")?.textContent).toContain("\\begin{aligned}");
    expect(screen.queryByRole("button", { name: "Copy code" })).toBeNull();
  });

  it("shows an open \\[ block as math source while streaming", () => {
    const { container } = render(<ChatMarkdown text={"Text\n\n\\[\n\\frac{a_1}{b"} streaming />);
    noMath(container);
    expect(container.querySelector("pre")?.textContent).toContain("\\frac{a_1}{b");
  });
});

describe("a price mid-stream", () => {
  it("never flashes as a formula when the tail ends at a $", () => {
    const { container, rerender } = render(<ChatMarkdown text="From $5-$" streaming />);
    noMath(container);
    rerender(<ChatMarkdown text="From $5-$10" streaming />);
    noMath(container);
    expect(container.textContent).toContain("From $5-$10");
  });

  it("still renders $x$ once the next character or the end arrives", async () => {
    const { container, rerender } = render(<ChatMarkdown text="so $x$" streaming />);
    noMath(container);
    rerender(<ChatMarkdown text="so $x$." streaming />);
    await katexNodes(container);
    const done = render(<ChatMarkdown text="so $x$" />);
    await katexNodes(done.container);
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

  it("keeps a $$ block with TeX on its fence lines together", () => {
    const text = "$$\\begin{aligned}\na\n\nb\n\\end{aligned}$$\n\nAfter";
    expect(splitBlocks(text)).toEqual([
      "$$\\begin{aligned}\na\n\nb\n\\end{aligned}$$\n\n",
      "After",
    ]);
  });

  it("leaves the inside of an open $$ block alone", () => {
    // `**` inside TeX is no emphasis to close.
    expect(stabilizeTail("$$\na**b")).not.toContain("a**b**");
  });
});

describe("\\[ inside a code fence", () => {
  it("is code, not a formula left open, while the answer streams", () => {
    const text = "```latex\n\\[ a\n```";
    expect(stabilizeTail(text)).toBe(text);
    const { container } = render(<ChatMarkdown text={text} streaming />);
    expect(container.querySelector("code")?.textContent).toContain("\\[ a");
    expect(container.textContent).not.toContain("math-source");
  });

  it("still finds an open \\[ after a closed fence", () => {
    expect(stabilizeTail("```\nx\n```\n\n\\[\n\\frac{a}{b")).toContain("```math-source");
  });
});

describe("display math in a list item or a quote", () => {
  it.each([
    ["a list item", "- $$\\begin{aligned}\na &= b\n\\end{aligned}$$", "li"],
    ["a numbered item", "1. $$\\begin{aligned}\n   a &= b\n   \\end{aligned}$$", "li"],
    ["a quote", "> $$\\begin{aligned}\n> a &= b\n> \\end{aligned}$$", "blockquote"],
  ])("renders as display math in %s", async (_, text, container_) => {
    const { container } = render(<ChatMarkdown text={text} />);
    await katexNodes(container);
    const display = container.querySelector(`${container_} .katex-display`);
    // KaTeX keeps the source in its annotation: the environment reached it.
    expect(display?.textContent).toContain("\\begin{aligned}");
    const outside = container.cloneNode(true) as HTMLElement;
    outside.querySelectorAll(".katex").forEach((node) => node.remove());
    expect(outside.textContent).not.toContain("aligned");
  });
});
