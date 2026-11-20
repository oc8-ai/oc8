// A rehype step used only on the block an answer is still writing: the text
// revealed since the last reveal (from source offset `from` on) is wrapped in
// <span class="stream-fresh" data-fresh={from}>, and nothing else is touched.
// Words already on screen stay plain text nodes, so they never fade again;
// the fresh span gets a new key per reveal (see ChatMarkdown's `span`), so
// its short fade plays once for exactly the words that just appeared. One
// span per text node in the fresh range -- usually one -- instead of a span
// per word. Code stays untouched: the highlighter reads its text.

interface Position {
  start?: { offset?: number };
  end?: { offset?: number };
}

interface HastText {
  type: "text";
  value: string;
  position?: Position;
}

interface HastElement {
  type: "element";
  tagName: string;
  properties: Record<string, unknown>;
  children: HastNode[];
  position?: Position;
}

type HastNode = HastText | HastElement | { type: string; children?: HastNode[] };

const SKIP = new Set(["pre", "code", "script", "style"]);

function fresh(value: string, from: number): HastElement {
  return {
    type: "element",
    tagName: "span",
    properties: { className: ["stream-fresh"], dataFresh: String(from) },
    children: [{ type: "text", value }],
  };
}

function walk(node: { children?: HastNode[] }, from: number): void {
  if (!node.children) return;
  const next: HastNode[] = [];
  for (const child of node.children) {
    if (child.type !== "text") {
      if (!(child.type === "element" && SKIP.has((child as HastElement).tagName))) {
        walk(child as { children?: HastNode[] }, from);
      }
      next.push(child);
      continue;
    }
    const text = child as HastText;
    const start = text.position?.start?.offset;
    const end = text.position?.end?.offset;
    // No position (a node a plugin made), only whitespace, or wholly before
    // the cut: as is.
    if (start === undefined || end === undefined || end <= from || !text.value.trim()) {
      next.push(child);
      continue;
    }
    // The source can be longer than the text (escapes, entities); the cut
    // is clamped, a character early or late is invisible.
    const cut = Math.min(Math.max(from - start, 0), text.value.length);
    if (cut > 0) next.push({ type: "text", value: text.value.slice(0, cut) });
    if (cut < text.value.length) next.push(fresh(text.value.slice(cut), from));
  }
  node.children = next;
}

/** `rehypePlugins={[[rehypeStreamFresh, { from }]]}`. */
export function rehypeStreamFresh(options: { from: number }) {
  return (tree: unknown) => {
    walk(tree as { children?: HastNode[] }, options.from);
  };
}
