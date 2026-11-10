// A rehype step used only while an answer streams: each word of prose becomes
// <span class="stream-word">, so a word that just arrived fades in (see
// `@utility stream-tail`). React keys an element by tag and position, so the
// words already on screen keep their node and do not fade again; only the new
// ones mount. Code stays untouched -- the highlighter reads its text.

interface HastText {
  type: "text";
  value: string;
}

interface HastElement {
  type: "element";
  tagName: string;
  properties: Record<string, unknown>;
  children: HastNode[];
}

type HastNode = HastText | HastElement | { type: string; children?: HastNode[] };

const SKIP = new Set(["pre", "code", "script", "style"]);

function wrap(value: string): HastNode[] {
  return value
    .split(/(\s+)/)
    .filter((part) => part !== "")
    .map((part) =>
      /^\s+$/.test(part)
        ? ({ type: "text", value: part } satisfies HastText)
        : ({
            type: "element",
            tagName: "span",
            properties: { className: ["stream-word"] },
            children: [{ type: "text", value: part }],
          } satisfies HastElement),
    );
}

function walk(node: { children?: HastNode[] }): void {
  if (!node.children) return;
  const next: HastNode[] = [];
  for (const child of node.children) {
    if (child.type === "text") {
      next.push(...wrap((child as HastText).value));
    } else {
      if (!(child.type === "element" && SKIP.has((child as HastElement).tagName))) {
        walk(child as { children?: HastNode[] });
      }
      next.push(child);
    }
  }
  node.children = next;
}

/** The rehype plugin: `rehypePlugins={[rehypeStreamWords]}`. */
export function rehypeStreamWords() {
  return (tree: unknown) => {
    walk(tree as { children?: HastNode[] });
  };
}
