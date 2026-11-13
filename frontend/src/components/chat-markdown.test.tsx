import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { ChatMarkdown } from "@/components/chat-markdown";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn() } }));

const CONNECTED = "odoo.example.com";
const ISSUED = "https://odoo.example.com/odoo/sale.order/42";
const GUESSED = "https://odoo.example.com/odoo/sale.order/99";

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
const render = (ui: ReactElement) => rtlRender(ui, { wrapper });

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(api.get)
    .mockReset()
    .mockResolvedValue({ hosts: [CONNECTED] });
  vi.mocked(api.post)
    .mockReset()
    .mockImplementation(async (_path, body) => ({
      issued: (body as { urls: string[] }).urls.filter((u) => u === ISSUED),
    }));
});

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
  it("opens http(s) links in a new tab with noopener and an external icon", async () => {
    render(<ChatMarkdown text="[docs](https://example.com)" />);
    const link = await screen.findByRole("link", { name: /docs/ });
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

describe("ChatMarkdown record links", () => {
  it("does not make links clickable while the answer is still streaming", async () => {
    const md = `[S00042](${ISSUED})`;
    const { container, rerender } = render(<ChatMarkdown text={md} streaming />);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("S00042");
    rerender(<ChatMarkdown text={md} />);
    await waitFor(() => expect(container.querySelector("a")?.getAttribute("href")).toBe(ISSUED));
  });

  it("keeps links text in a live answer that has not been vetted yet", async () => {
    const { container } = render(
      <ChatMarkdown text={`[S00042](${ISSUED}) [docs](https://example.com)`} unvetted />,
    );
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("docs");
  });

  it("links to a connected system only what oc8 verified, and asks once", async () => {
    render(<ChatMarkdown text={`[issued](${ISSUED}) [guessed](${GUESSED})`} />);
    expect(await screen.findByRole("link", { name: /issued/ })).toHaveAttribute("href", ISSUED);
    expect(screen.queryByRole("link", { name: /guessed/ })).toBeNull();
    expect(screen.getByText("guessed").tagName).toBe("SPAN");
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith("/record-links/verify", {
      urls: [ISSUED, GUESSED].sort(),
    });
  });

  it("leaves links to other hosts clickable without asking", async () => {
    render(<ChatMarkdown text="[docs](https://example.com/x)" />);
    expect(await screen.findByRole("link", { name: /docs/ })).toHaveAttribute(
      "href",
      "https://example.com/x",
    );
    expect(api.post).not.toHaveBeenCalled();
  });

  it("shows no http link at all while the connected hosts are unknown", async () => {
    vi.mocked(api.get).mockReset().mockRejectedValue(new Error("down"));
    const { container } = render(
      <ChatMarkdown text={`[docs](https://example.com/x) [issued](${ISSUED})`} />,
    );
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("docs");
  });

  it.each([
    ["a backslash path", "https://odoo.example.com\\odoo\\99"],
    ["userinfo before the host", "https://example.com\\@odoo.example.com/odoo/99"],
    ["a respelled host", "https://ODOO.example.com./odoo/sale.order/99"],
    ["a percent-encoded dot", "https://odoo%2Eexample.com/odoo/sale.order/99"],
  ])("judges %s by the host the browser opens", async (_name, href) => {
    const { container } = render(
      <ChatMarkdown text={`[docs](https://example.com/x) [record](${href})`} />,
    );
    await screen.findByRole("link", { name: /docs/ });
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelectorAll("a")).toHaveLength(1);
    expect(container.textContent).toContain("record");
  });

  it("keeps oc8 object links and citations unchanged", async () => {
    render(<ChatMarkdown text="See [Lennart](oc8://agent/abc)." />);
    expect(screen.getByRole("link", { name: "Lennart" })).toBeInTheDocument();
  });
});
