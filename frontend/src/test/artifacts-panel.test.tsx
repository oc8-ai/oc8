// An answer's artifacts as cards that open beside the chat (spec §5.2):
// Preview | Code, copy, download, versions on the message path, a sheet
// under 1024 px; previews never run in the page (§4.1).
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mermaid = vi.hoisted(() => ({
  initialize: vi.fn(),
  parse: vi.fn(async () => ({ diagramType: "flowchart" })),
  render: vi.fn(async () => ({
    svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>Drawn</text></svg>',
  })),
}));
vi.mock("mermaid", () => ({ default: mermaid }));
vi.mock("shiki", () => ({
  codeToHtml: async (code: string) =>
    `<pre class="shiki"><code>${code.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</code></pre>`,
}));
vi.mock("@/lib/record-links", async (orig) => ({
  ...(await orig<typeof import("@/lib/record-links")>()),
  useVerifiedLinks: () => ({ data: undefined }),
}));

import { ChatMarkdown } from "@/components/chat-markdown";
import { ArtifactPanel } from "@/components/conversation/artifacts/artifact-panel";
import {
  ArtifactsContext,
  liveArtifactsId,
  useArtifactsPanel,
  usePanelAsSheet,
} from "@/components/conversation/artifacts/context";
import type { TurnModel } from "@/components/conversation/types";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

const page = (title: string, body: string) =>
  "```html\n<!doctype html>\n<title>" + title + "</title>\n" + body + "\n```";

function message(id: string, content: string, runId: string | null = null): ChatMessageDTO {
  return { id, content, runId, role: "assistant" } as unknown as ChatMessageDTO;
}

function answer(m: ChatMessageDTO): TurnModel {
  return { kind: "assistant", key: m.id, replyToId: null, variants: [m], liveRunId: null };
}

/** The conversation's wiring, without the rest of the conversation: each
 *  turn's answer through ChatMarkdown, the live one last, and the panel. */
function Chat({
  turns,
  live,
  wide = true,
}: {
  turns: TurnModel[];
  live?: { runId: string; text: string };
  wide?: boolean;
}) {
  const panel = useArtifactsPanel("s1", turns);
  const narrow = usePanelAsSheet();
  return (
    <ArtifactsContext.Provider value={panel.context}>
      {turns.map((turn) =>
        turn.kind === "assistant" ? (
          <ChatMarkdown
            key={turn.key}
            text={turn.variants[0].content}
            messageId={turn.variants[0].id}
            artifactsOf={turn.variants[0].id}
          />
        ) : null,
      )}
      {live && (
        <ChatMarkdown text={live.text} streaming artifactsOf={liveArtifactsId(live.runId)} />
      )}
      <ArtifactPanel
        artifact={panel.current}
        versions={panel.versions}
        onSelect={panel.open}
        onClose={panel.close}
        asSheet={narrow || !wide}
      />
    </ArtifactsContext.Provider>
  );
}

function stubWidth(px: number) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: /max-width: (\d+)px/.test(query) && px <= Number(/max-width: (\d+)px/.exec(query)![1]),
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

beforeEach(() => stubWidth(1440));
afterEach(() => vi.restoreAllMocks());

describe("artifact cards", () => {
  it("shows an html block as a card and opens it in a sandboxed preview", () => {
    render(<Chat turns={[answer(message("m1", "Here:\n\n" + page("Clock", "<p>12:00</p>")))]} />);
    const card = screen.getByRole("button", { name: "Open Clock" });
    expect(card).toHaveTextContent("HTML · 3 lines");
    // The block itself is not in the answer.
    expect(screen.queryByText(/<p>12:00<\/p>/)).toBeNull();
    fireEvent.click(card);
    expect(card).toHaveAttribute("aria-pressed", "true");
    const panel = screen.getByRole("complementary", { name: "Clock" });
    const frame = within(panel).getByTitle("Clock");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("srcdoc")).toContain("<p>12:00</p>");
    fireEvent.click(within(panel).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("complementary")).toBeNull();
  });

  it("keeps short code inline and shows long code as a card with a Code view only", async () => {
    const short = "```js\nshort()\n```";
    const long = "```python\n# Report builder\n" + "x = 1\n".repeat(20) + "```";
    render(<Chat turns={[answer(message("m1", short + "\n\n" + long))]} />);
    expect(screen.getByText("short()")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Report builder" }));
    const panel = screen.getByRole("complementary");
    expect(within(panel).queryByRole("tab")).toBeNull();
    // Highlighted once Shiki is there.
    await waitFor(() =>
      expect(panel.querySelector(".shiki")).toHaveTextContent("# Report builder"),
    );
  });

  it("without a conversation, an answer shows its blocks as before", () => {
    render(<ChatMarkdown text={page("Clock", "<p>12:00</p>")} artifactsOf="m1" />);
    expect(screen.queryByTestId("artifact-card")).toBeNull();
    expect(screen.getByText(/<p>12:00<\/p>/)).toBeInTheDocument();
  });
});

describe("while the answer streams", () => {
  it("says writing… and previews only once the block is closed", () => {
    const open = "Building:\n\n```html\n<title>Clock</title>\n<p>12";
    const { rerender } = render(<Chat turns={[]} live={{ runId: "r1", text: open }} />);
    const card = screen.getByRole("button", { name: "Open Clock" });
    expect(card).toHaveTextContent("writing…");
    fireEvent.click(card);
    const panel = screen.getByRole("complementary");
    expect(within(panel).getByRole("status")).toHaveTextContent("Writing…");
    expect(panel.querySelector("iframe")).toBeNull();

    rerender(<Chat turns={[]} live={{ runId: "r1", text: open + ":00</p>\n```\n\nDone." }} />);
    expect(screen.getByRole("button", { name: "Open Clock" })).toHaveTextContent("HTML");
    const frame = screen.getByTitle("Clock");
    expect(frame.getAttribute("srcdoc")).toContain("<p>12:00</p>");
  });

  it("stays open on the saved answer once it lands", () => {
    const text = page("Clock", "<p>12:00</p>");
    const { rerender } = render(<Chat turns={[]} live={{ runId: "r1", text }} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Clock" }));
    rerender(<Chat turns={[answer(message("m9", text, "r1"))]} />);
    expect(screen.getByRole("complementary", { name: "Clock" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Clock" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("the panel", () => {
  it("switches between Preview and Code and copies the source", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<Chat turns={[answer(message("m1", page("Clock", "<p>12:00</p>")))]} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Clock" }));
    const panel = screen.getByRole("complementary");
    expect(within(panel).getByRole("tab", { name: "Preview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.mouseDown(within(panel).getByRole("tab", { name: "Code" }));
    await waitFor(() => expect(panel.querySelector(".shiki")).toHaveTextContent("<p>12:00</p>"));
    expect(panel.querySelector("iframe")).toBeNull();
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "Copy" }));
    });
    expect(writeText).toHaveBeenCalledWith("<!doctype html>\n<title>Clock</title>\n<p>12:00</p>");
  });

  it("downloads the source under the title's file name", () => {
    const createObjectURL = vi.fn(() => "blob:x");
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() }));
    const clicked: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(this);
    });
    render(<Chat turns={[answer(message("m1", page("Sales Board", "<p>x</p>")))]} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Sales Board" }));
    fireEvent.click(screen.getByRole("button", { name: "Download sales-board.html" }));
    expect(clicked).toHaveLength(1);
    expect(clicked[0].download).toBe("sales-board.html");
    const blob = (createObjectURL.mock.calls[0] as unknown as [Blob])[0];
    expect(blob.type).toBe("text/html;charset=utf-8");
  });

  it("steps through the versions with the same title on the path", () => {
    render(
      <Chat
        turns={[
          answer(message("m1", page("Clock", "<p>v1</p>"))),
          answer(message("m2", page("Other", "<p>o</p>"))),
          answer(message("m3", page("Clock", "<p>v2</p>"))),
        ]}
      />,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Open Clock" })[1]);
    const panel = screen.getByRole("complementary");
    expect(within(panel).getByText("2/2")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Next version" })).toBeDisabled();
    expect(within(panel).getByTitle("Clock").getAttribute("srcdoc")).toContain("<p>v2</p>");
    fireEvent.click(within(panel).getByRole("button", { name: "Previous version" }));
    expect(within(panel).getByText("1/2")).toBeInTheDocument();
    expect(within(panel).getByTitle("Clock").getAttribute("srcdoc")).toContain("<p>v1</p>");
    // The answer's card for that version is the open one now.
    const [first, second] = screen.getAllByRole("button", { name: "Open Clock" });
    expect(first).toHaveAttribute("aria-pressed", "true");
    expect(second).toHaveAttribute("aria-pressed", "false");
  });

  it("is a sheet under 1024 px", () => {
    stubWidth(1023);
    render(<Chat turns={[answer(message("m1", page("Clock", "<p>x</p>")))]} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Clock" }));
    expect(screen.queryByRole("complementary")).toBeNull();
    const sheet = screen.getByRole("dialog", { name: "Clock" });
    expect(within(sheet).getByTitle("Clock").getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("shows SVG as an image, never inline", () => {
    const svg =
      '```svg\n<svg xmlns="http://www.w3.org/2000/svg"><title>Logo</title><script>alert(1)</script></svg>\n```';
    const { container } = render(<Chat turns={[answer(message("m1", svg))]} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Logo" }));
    const img = screen.getByRole("img", { name: "Logo" });
    expect(img.getAttribute("src")).toMatch(/^data:image\/svg\+xml/);
    expect(document.querySelector("svg title")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("draws Mermaid in the page, in strict mode, as an image", async () => {
    const diagram = "```mermaid\n%% Order flow\ngraph TD; A-->B\n```";
    render(<Chat turns={[answer(message("m1", diagram))]} />);
    // The diagram stays inline in the answer and opens beside it.
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    const panel = screen.getByRole("complementary", { name: "Order flow" });
    const img = await within(panel).findByRole("img", { name: "Diagram" });
    expect(decodeURIComponent(img.getAttribute("src")!)).toContain("<text>Drawn</text>");
    expect(mermaid.initialize).toHaveBeenCalledWith(
      expect.objectContaining({ securityLevel: "strict" }),
    );
  });
});
