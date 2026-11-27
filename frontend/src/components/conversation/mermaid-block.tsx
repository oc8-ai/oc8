// A ```mermaid block: drawn as a diagram once the block is closed, its
// source while it is still being written or when Mermaid cannot parse it.
//
// Mermaid is its own chunk, fetched the first time a diagram is drawn. It
// runs with `securityLevel: "strict"` (no click handlers, labels sanitised)
// and SVG text labels instead of HTML ones, and its SVG is shown as an
// <img> data URL: nothing of it joins the page's DOM, and an image runs no
// script and loads nothing.

import { useEffect, useState } from "react";
import { Check, Code, Copy, Network } from "lucide-react";

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Mermaid = (typeof import("mermaid"))["default"];

let loading: Promise<Mermaid> | null = null;

function loadMermaid(): Promise<Mermaid> {
  loading ??= import("mermaid").then(
    ({ default: mermaid }) => {
      return mermaid;
    },
    (error: unknown) => {
      // A failed chunk fetch may succeed on the next diagram.
      loading = null;
      throw error;
    },
  );
  return loading;
}

// Mermaid renders through shared state (a scratch element in the document),
// so diagrams are drawn one after another.
let queue: Promise<unknown> = Promise.resolve();
let drawn = 0;

/** The SVG Mermaid draws for `source`, in the page's light or dark colours
 *  as they are now; rejects when it does not parse. */
export function renderMermaidToSvg(source: string): Promise<string> {
  const run = queue.then(async () => {
    const mermaid = await loadMermaid();
    const dark =
      typeof document !== "undefined" && document.documentElement.classList.contains("dark");
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      htmlLabels: false,
      theme: dark ? "dark" : "default",
    });
    // Parse first: a failed render would leave its error diagram behind.
    await mermaid.parse(source);
    drawn += 1;
    const { svg } = await mermaid.render(`oc8-mermaid-${drawn}`, source);
    return svg;
  });
  queue = run.catch(() => undefined);
  return run;
}

function svgDataUrl(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

const FOCUS =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";
const BUTTON = cn("inline-flex items-center gap-1 rounded-md px-1 hover:text-foreground", FOCUS);

export function MermaidBlock({ source, closed }: { source: string; closed: boolean }) {
  const t = useT();
  // What Mermaid made of a source; one for another source is stale.
  const [result, setResult] = useState<{ source: string; svg?: string; failed?: boolean }>();
  const [showSource, setShowSource] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!closed) return;
    let alive = true;
    renderMermaidToSvg(source).then(
      (svg) => {
        if (alive) setResult({ source, svg });
      },
      () => {
        if (alive) setResult({ source, failed: true });
      },
    );
    return () => {
      alive = false;
    };
  }, [source, closed]);

  const current = closed && result?.source === source ? result : undefined;
  const svg = current?.svg;
  const diagram = svg && !showSource;

  function copy() {
    // navigator.clipboard is undefined outside a secure context.
    if (!navigator.clipboard) return;
    navigator.clipboard
      .writeText(source)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => undefined);
  }

  return (
    <div className="overflow-hidden rounded-md border border-border bg-muted/40">
      <div className="flex items-center justify-between gap-2 px-3 py-1 text-xs text-muted-foreground">
        <span className="font-mono">mermaid</span>
        <span className="flex items-center gap-2">
          {svg && (
            <button type="button" onClick={() => setShowSource((s) => !s)} className={BUTTON}>
              {showSource ? (
                <Network className="size-3.5" aria-hidden />
              ) : (
                <Code className="size-3.5" aria-hidden />
              )}
              {showSource
                ? t("Show diagram", "Diagramm anzeigen")
                : t("Show source", "Quelltext anzeigen")}
            </button>
          )}
          <button
            type="button"
            onClick={copy}
            className={BUTTON}
            aria-label={t("Copy code", "Code kopieren")}
          >
            {copied ? (
              <Check className="size-3.5" aria-hidden />
            ) : (
              <Copy className="size-3.5" aria-hidden />
            )}
            {copied ? t("Copied", "Kopiert") : t("Copy", "Kopieren")}
          </button>
        </span>
      </div>
      {current?.failed && (
        <p className="px-3 text-xs text-muted-foreground">
          {t("Diagram could not be drawn.", "Diagramm konnte nicht gezeichnet werden.")}
        </p>
      )}
      {diagram ? (
        <div className="overflow-x-auto bg-background p-3">
          <img
            src={svgDataUrl(svg)}
            alt={t("Diagram", "Diagramm")}
            className="mx-auto max-w-full"
          />
        </div>
      ) : (
        <pre className="overflow-x-auto px-3 py-2 font-mono text-xs">{source}</pre>
      )}
    </div>
  );
}
