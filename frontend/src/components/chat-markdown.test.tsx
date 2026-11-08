import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ChatMarkdown } from "@/components/chat-markdown";

describe("ChatMarkdown", () => {
  it("renders a fenced code block distinctly from inline code", () => {
    const { container } = render(<ChatMarkdown text={"```\nconst x = 1;\n```"} />);
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.className).toContain("overflow-x-auto");
  });

  it("renders a GFM table with horizontal-scroll overflow handling", () => {
    const table = "| A | B |\n| - | - |\n| 1 | 2 |";
    const { container } = render(<ChatMarkdown text={table} />);
    const tableEl = container.querySelector("table");
    expect(tableEl).not.toBeNull();
    expect(container.querySelector("th")).not.toBeNull();
  });

  it("shows a language label read from the fence info string", () => {
    const { container } = render(<ChatMarkdown text={"```ts\nconst x = 1;\n```"} />);
    expect(container.textContent).toContain("ts");
  });

  it("copies a code block's text to the clipboard via the copy button", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const { getByRole } = render(<ChatMarkdown text={"```\nconst x = 1;\n```"} />);
    fireEvent.click(getByRole("button", { name: /copy/i }));
    expect(writeText).toHaveBeenCalledWith("const x = 1;");
  });
});

vi.mock("shiki", () => ({
  codeToHtml: vi.fn(
    async (code: string) => `<pre class="shiki"><code>${code.replace(/</g, "&lt;")}</code></pre>`,
  ),
}));

describe("ChatMarkdown safety and features", () => {
  it("opens http(s) links in a new tab with noopener and an external icon", () => {
    render(<ChatMarkdown text="[docs](https://example.com)" />);
    const link = screen.getByRole("link", { name: /docs/ });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link.querySelector("svg")).not.toBeNull();
    expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(link).toHaveAccessibleName(/docs.*opens in a new tab/);
  });

  it("renders javascript: links as plain text", () => {
    render(<ChatMarkdown text="[click](javascript:alert(1))" />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("click")).toBeInTheDocument();
  });

  it("loads images only over https", () => {
    render(<ChatMarkdown text={"![a](http://x.test/a.png) ![b](https://x.test/b.png)"} />);
    expect(screen.queryAllByRole("img")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /Load image from x\.test/ }));
    const imgs = screen.getAllByRole("img");
    expect(imgs).toHaveLength(1);
    expect(imgs[0]).toHaveAttribute("src", "https://x.test/b.png");
    expect(screen.getByText("a")).toBeInTheDocument();
  });

  it("shows raw HTML as text", () => {
    const { container } = render(<ChatMarkdown text={"<script>alert(1)</script>"} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container).toHaveTextContent("<script>alert(1)</script>");
  });

  it("turns [n] into a citation when a source exists", () => {
    const onCitation = vi.fn();
    render(
      <ChatMarkdown
        text="Revenue grew [1] and [7]."
        sources={[
          {
            n: 1,
            title: "Q3 report",
            url: "https://r.test",
            favicon: null,
            snippet: "",
            kind: "web",
          },
        ]}
        onCitation={onCitation}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Source 1: Q3 report" }));
    expect(onCitation).toHaveBeenCalledWith(1);
    expect(screen.getByText(/\[7\]/)).toBeInTheDocument();
  });

  it("leaves [n] inside code alone", () => {
    const { container } = render(
      <ChatMarkdown
        text="Use `rows[1]` here."
        sources={[
          {
            n: 1,
            title: "Q3 report",
            url: "https://r.test",
            favicon: null,
            snippet: "",
            kind: "web",
          },
        ]}
      />,
    );
    expect(screen.queryByRole("button", { name: /Source 1/ })).toBeNull();
    expect(container.querySelector("code")).toHaveTextContent("rows[1]");
  });

  it("renders oc8 object references as chips linking to the object", () => {
    render(<ChatMarkdown text="See [Lennart](oc8://agent/abc)." />);
    const chip = screen.getByRole("link", { name: /Lennart/ });
    expect(chip).toHaveAttribute("href", "/agents/abc");
    expect(chip).not.toHaveAttribute("target");
  });

  it("highlights fenced code lazily with Shiki", async () => {
    render(<ChatMarkdown text={"```ts\nconst a = 1;\n```"} />);
    expect(
      await screen.findByText("const a = 1;", { selector: ".shiki code" }),
    ).toBeInTheDocument();
  });
});
