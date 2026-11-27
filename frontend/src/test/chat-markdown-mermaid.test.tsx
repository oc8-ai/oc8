// A ```mermaid block in an answer draws as a diagram (an <img> of the SVG
// Mermaid made, in strict mode), only once the block is closed; while it is
// still being written, or when Mermaid cannot parse it, the source shows.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mermaid = vi.hoisted(() => ({
  initialize: vi.fn(),
  parse: vi.fn(async () => ({ diagramType: "flowchart" })),
  render: vi.fn(async () => ({
    svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>A</text></svg>',
  })),
}));
vi.mock("mermaid", () => ({ default: mermaid }));

vi.mock("@/lib/record-links", async (orig) => ({
  ...(await orig<typeof import("@/lib/record-links")>()),
  useVerifiedLinks: () => ({ data: undefined }),
}));

import { ChatMarkdown } from "@/components/chat-markdown";
import { renderMermaidToSvg } from "@/components/conversation/mermaid-block";

const SOURCE = "graph TD\n  A-->B";
const CLOSED = "A diagram:\n\n```mermaid\n" + SOURCE + "\n```\n";
const OPEN = "A diagram:\n\n```mermaid\n" + SOURCE;

beforeEach(() => {
  mermaid.parse.mockClear();
  mermaid.render.mockClear();
});

describe("ChatMarkdown mermaid", () => {
  it("draws a closed block as an SVG image", async () => {
    const { container } = render(<ChatMarkdown text={CLOSED} />);
    const img = await screen.findByRole("img", { name: "Diagram" });
    expect(img.getAttribute("src")).toMatch(/^data:image\/svg\+xml/);
    expect(decodeURIComponent(img.getAttribute("src")!)).toContain("<text>A</text>");
    expect(mermaid.render).toHaveBeenCalledWith(expect.any(String), SOURCE);
    // Never inline: the SVG is an image, nothing of it joins the page's DOM.
    expect(container.querySelector("svg text")).toBeNull();
  });

  it("shows the code of an unclosed block while streaming", async () => {
    const { container } = render(<ChatMarkdown text={OPEN} streaming />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(mermaid.parse).not.toHaveBeenCalled();
    expect(mermaid.render).not.toHaveBeenCalled();
    expect(screen.queryByRole("img")).toBeNull();
    expect(container.textContent).toContain("A-->B");
  });

  it("draws the block once its closing fence arrives mid-stream", async () => {
    const { rerender } = render(<ChatMarkdown text={OPEN} streaming />);
    expect(mermaid.render).not.toHaveBeenCalled();
    rerender(<ChatMarkdown text={CLOSED + "\nMore words"} streaming />);
    await screen.findByRole("img", { name: "Diagram" });
    expect(mermaid.render).toHaveBeenCalledTimes(1);
  });

  it("shows the source and a note when the diagram cannot be parsed", async () => {
    mermaid.parse.mockRejectedValueOnce(new Error("Parse error on line 1"));
    const { container } = render(<ChatMarkdown text={CLOSED.replace("A-->B", "A-->")} />);
    await screen.findByText("Diagram could not be drawn.");
    expect(container.textContent).toContain("A-->");
    expect(screen.queryByRole("img")).toBeNull();
    expect(mermaid.render).not.toHaveBeenCalled();
  });

  it("switches between the diagram and its source", async () => {
    const { container } = render(<ChatMarkdown text={CLOSED} />);
    await screen.findByRole("img", { name: "Diagram" });
    fireEvent.click(screen.getByRole("button", { name: "Show source" }));
    expect(screen.queryByRole("img")).toBeNull();
    expect(container.querySelector("pre")?.textContent).toBe(SOURCE);
    fireEvent.click(screen.getByRole("button", { name: "Show diagram" }));
    expect(screen.getByRole("img", { name: "Diagram" })).toBeInTheDocument();
  });

  it("copies the source", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<ChatMarkdown text={CLOSED} />);
    await screen.findByRole("img", { name: "Diagram" });
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SOURCE));
  });
});

describe("renderMermaidToSvg", () => {
  it("runs Mermaid in strict mode without HTML labels", async () => {
    const svg = await renderMermaidToSvg("graph LR\n  X-->Y");
    expect(svg).toContain("<svg");
    expect(mermaid.initialize).toHaveBeenCalledWith(
      expect.objectContaining({ securityLevel: "strict", htmlLabels: false, startOnLoad: false }),
    );
  });

  it("rejects when the source does not parse", async () => {
    mermaid.parse.mockRejectedValueOnce(new Error("bad"));
    await expect(renderMermaidToSvg("nope")).rejects.toThrow("bad");
  });
});
