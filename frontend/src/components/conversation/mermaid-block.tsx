// A ```mermaid block: drawn as a diagram once the block is closed (see
// mermaid-render.ts), its source while it is still being written or when
// Mermaid cannot parse it. In a conversation it also opens beside the chat. Drawn again in the page's colours when the theme
// changes.

import { useEffect, useState } from "react";
import { Check, Code, Copy, Maximize2, Network } from "lucide-react";

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { pageIsDark, renderMermaidToSvg, svgDataUrl } from "./mermaid-render";

/** Whether the page shows its dark colours, kept current: the theme toggle
 *  flips `.dark` on <html> (see routes/__root.tsx). */
function usePageDark(): boolean {
  const [dark, setDark] = useState(pageIsDark);
  useEffect(() => {
    const sync = () => setDark(pageIsDark());
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return dark;
}

const FOCUS =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";
const BUTTON = cn("inline-flex items-center gap-1 rounded-md px-1 hover:text-foreground", FOCUS);

export function MermaidBlock({
  source,
  closed,
  onOpen,
}: {
  source: string;
  closed: boolean;
  /** Opens the diagram beside the chat (in a conversation). */
  onOpen?: () => void;
}) {
  const t = useT();
  // What Mermaid made of a source; one for another source is stale.
  const [result, setResult] = useState<{ source: string; svg?: string; failed?: boolean }>();
  const [showSource, setShowSource] = useState(false);
  const [copied, setCopied] = useState(false);
  const dark = usePageDark();

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
    // `dark` is read by renderMermaidToSvg: a new theme draws it again, and
    // the old drawing stays until the new one is there.
  }, [source, closed, dark]);

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
          {onOpen && svg && (
            <button type="button" onClick={onOpen} className={BUTTON}>
              <Maximize2 className="size-3.5" aria-hidden />
              {t("Open", "Öffnen")}
            </button>
          )}
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
