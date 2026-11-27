// Mermaid source to SVG, for a ```mermaid block in an answer (MermaidBlock)
// and for a diagram opened beside the chat.
//
// Mermaid is its own chunk, fetched the first time a diagram is drawn. It
// runs with `securityLevel: "strict"` (no click handlers, labels sanitised)
// and SVG text labels instead of HTML ones. Show its SVG as an <img> data URL
// (`svgDataUrl`): nothing of it joins the page's DOM, and an image runs no
// script and loads nothing.

type Mermaid = (typeof import("mermaid"))["default"];

let loading: Promise<Mermaid> | null = null;

function loadMermaid(): Promise<Mermaid> {
  loading ??= import("mermaid").then(
    ({ default: mermaid }) => mermaid,
    (error: unknown) => {
      // A failed chunk fetch may succeed on the next diagram.
      loading = null;
      throw error;
    },
  );
  return loading;
}

/** Whether the page shows its dark colours (`.dark` on <html>). */
export function pageIsDark(): boolean {
  return typeof document !== "undefined" && document.documentElement.classList.contains("dark");
}

// Mermaid renders through shared state (its config, a scratch element in the
// document), so diagrams are drawn one after another.
let queue: Promise<unknown> = Promise.resolve();
let drawn = 0;

/** The SVG Mermaid draws for `source`, in the page's light or dark colours
 *  as they are now; rejects when it does not parse. */
export function renderMermaidToSvg(source: string): Promise<string> {
  const run = queue.then(async () => {
    const mermaid = await loadMermaid();
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      htmlLabels: false,
      theme: pageIsDark() ? "dark" : "default",
    });
    // Parse first: a failed render would leave its error diagram behind.
    await mermaid.parse(source);
    drawn += 1;
    const { svg } = await mermaid.render(`oc8-mermaid-${drawn}`, source);
    return withNaturalSize(svg);
  });
  queue = run.catch(() => undefined);
  return run;
}

/** Mermaid's root `<svg>` says `width="100%"` (its real width only in a
 *  `max-width` style), which as an <img> has no natural width: a small
 *  diagram then fills its container. Size the root element from its viewBox
 *  instead, so the image has its natural size and CSS only scales it down. */
function withNaturalSize(svg: string): string {
  const root = /^<svg\b[^>]*>/.exec(svg)?.[0];
  const box = root && /\sviewBox="[-\d.e]+[\s,]+[-\d.e]+[\s,]+([\d.e]+)[\s,]+([\d.e]+)"/.exec(root);
  if (!root || !box || !/\swidth="100%"/.test(root)) return svg;
  const sized = root
    .replace(/\sheight="[^"]*"/, "")
    .replace(/\swidth="100%"/, ` width="${box[1]}" height="${box[2]}"`);
  return sized + svg.slice(root.length);
}

/** An SVG as an <img> source. */
export function svgDataUrl(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
