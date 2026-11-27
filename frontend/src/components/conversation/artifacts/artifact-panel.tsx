// The open artifact, right of the conversation (a sheet under 1024 px and in
// the compact chat): Preview | Code, copy, download, and a stepper through
// its versions -- the artifacts with its title on the current message path.
//
// Previews never run in this page: HTML in a SandboxFrame, SVG and Mermaid
// (drawn here, strict mode) as <img> data URLs, which run no script and load
// nothing. While the block is still being written the preview waits for it
// to close, so a half page or diagram never shows. Python code has Run in
// its Code tab (python/run-output.tsx), under the same rule as scripts.

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Copy, Download, X } from "lucide-react";

import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { pageIsDark, renderMermaidToSvg, svgDataUrl } from "../mermaid-render";
import { isPythonLanguage } from "../python/run-context";
import { PythonRun } from "../python/run-output";
import type { PreviewMode } from "./context";
import { fileNameFor, kindLabel, type Artifact } from "./extract";
import { SandboxFrame } from "./sandbox-frame";

// HTML and SVG download as bytes: a browser that opened the blob instead of
// saving it must not render an untrusted page in the oc8 origin.
const MIME: Record<Artifact["kind"], string> = {
  html: "application/octet-stream",
  svg: "application/octet-stream",
  mermaid: "text/plain",
  code: "text/plain",
};

function download(artifact: Artifact) {
  const url = URL.createObjectURL(
    new Blob([artifact.source], {
      type:
        artifact.kind === "html" || artifact.kind === "svg"
          ? MIME[artifact.kind]
          : `${MIME[artifact.kind]};charset=utf-8`,
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = fileNameFor(artifact);
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

const ICON_BUTTON =
  "grid size-9 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50";

function Writing() {
  const t = useT();
  return (
    <div className="space-y-2 p-4" role="status">
      <p className="text-shimmer text-xs text-muted-foreground">
        {t(
          "Writing… the preview appears once it is complete.",
          "Wird geschrieben … die Vorschau erscheint, sobald es fertig ist.",
        )}
      </p>
      <Skeleton className="h-4 w-2/3 rounded-md" />
      <Skeleton className="h-4 w-1/2 rounded-md" />
    </div>
  );
}

/** A Mermaid diagram as an image, drawn in this page in strict mode. */
function MermaidPreview({ source }: { source: string }) {
  const t = useT();
  const [result, setResult] = useState<{ source: string; svg?: string; failed?: boolean }>();
  useEffect(() => {
    let alive = true;
    renderMermaidToSvg(source).then(
      (svg) => alive && setResult({ source, svg }),
      () => alive && setResult({ source, failed: true }),
    );
    return () => {
      alive = false;
    };
  }, [source]);
  const current = result?.source === source ? result : undefined;
  if (current?.failed) {
    return (
      <p className="p-4 text-xs text-muted-foreground">
        {t("Diagram could not be drawn.", "Diagramm konnte nicht gezeichnet werden.")}
      </p>
    );
  }
  if (!current?.svg) return <Skeleton className="m-4 h-40 rounded-md" />;
  return (
    <img
      src={svgDataUrl(current.svg)}
      alt={t("Diagram", "Diagramm")}
      className="mx-auto max-w-full p-4"
    />
  );
}

/** Why a page shows without its scripts (see PreviewMode). */
function StaticNote({ mode }: { mode: PreviewMode }) {
  const t = useT();
  const text =
    mode === "restricted"
      ? t(
          "Shown without scripts because this answer contains restricted content.",
          "Ohne Skripte angezeigt, weil diese Antwort eingeschränkte Inhalte enthält.",
        )
      : mode === "off"
        ? t(
            "Shown without scripts: scripts in previews are turned off for your organization.",
            "Ohne Skripte angezeigt: Skripte in Vorschauen sind für deine Organisation ausgeschaltet.",
          )
        : mode === "unknown"
          ? t(
              "Shown without scripts: the preview setting could not be read.",
              "Ohne Skripte angezeigt: Die Vorschau-Einstellung konnte nicht gelesen werden.",
            )
          : t(
              "Shown without scripts until the answer is saved.",
              "Ohne Skripte angezeigt, bis die Antwort gespeichert ist.",
            );
  return (
    <p role="note" className="border-b border-border px-4 py-2 text-xs text-muted-foreground">
      {text}
    </p>
  );
}

function Preview({ artifact, mode }: { artifact: Artifact; mode: PreviewMode }) {
  const t = useT();
  if (!artifact.closed) return <Writing />;
  if (artifact.kind === "html") {
    if (mode === "pending") return <Skeleton className="m-4 h-40 rounded-md" />;
    const scripts = mode === "scripts";
    return (
      <div className="flex size-full flex-col">
        {!scripts && <StaticNote mode={mode} />}
        <SandboxFrame
          // A new mode is a new document: the frame starts over.
          key={scripts ? "scripts" : "static"}
          html={artifact.source}
          title={artifact.title}
          scripts={scripts}
          className="min-h-0 w-full flex-1"
        />
      </div>
    );
  }
  if (artifact.kind === "svg") {
    return (
      <div className="p-4">
        <img
          src={svgDataUrl(artifact.source)}
          alt={artifact.title || t("Image", "Bild")}
          className="mx-auto max-w-full"
        />
      </div>
    );
  }
  if (artifact.kind === "mermaid") return <MermaidPreview source={artifact.source} />;
  return null;
}

/** The source, highlighted once complete (Shiki, as in the answer). */
function Source({ artifact }: { artifact: Artifact }) {
  const [html, setHtml] = useState<{ source: string; html: string } | null>(null);
  const lang = artifact.kind === "code" ? artifact.language || "text" : artifact.kind;
  const { source, closed } = artifact;
  useEffect(() => {
    if (!closed) return;
    let alive = true;
    void import("shiki")
      .then(({ codeToHtml }) =>
        codeToHtml(source, {
          lang: lang === "mermaid" ? "text" : lang,
          themes: { light: "github-light", dark: "github-dark" },
          defaultColor: false,
        }),
      )
      .then(
        (out) => alive && setHtml({ source, html: out }),
        () => undefined,
      )
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [source, closed, lang]);
  const highlighted = useMemo(
    () => (html && html.source === source ? { __html: html.html } : null),
    [html, source],
  );
  return highlighted ? (
    // Shiki escapes the code it highlights; its output is markup it built.
    <div
      className="px-4 py-3 font-mono text-xs [&_pre]:bg-transparent"
      dangerouslySetInnerHTML={highlighted}
    />
  ) : (
    <pre className="px-4 py-3 font-mono text-xs">{source}</pre>
  );
}

function PanelBody({
  artifact,
  versions,
  mode,
  onSelect,
  onClose,
  asSheet,
}: {
  artifact: Artifact;
  versions: Artifact[];
  mode: PreviewMode;
  onSelect: (artifact: Artifact) => void;
  onClose: () => void;
  asSheet: boolean;
}) {
  const t = useT();
  const previewable = artifact.kind !== "code";
  const [tab, setTab] = useState<"preview" | "code">(previewable ? "preview" : "code");
  const shownTab = previewable ? tab : "code";
  const [copied, setCopied] = useState(false);
  const index = Math.max(
    0,
    versions.findIndex((v) => v.id === artifact.id),
  );
  // A theme change draws a diagram again in the new colours.
  const [dark, setDark] = useState(pageIsDark);
  useEffect(() => {
    const observer = new MutationObserver(() => setDark(pageIsDark()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  function copy() {
    // navigator.clipboard is undefined outside a secure context.
    if (!navigator.clipboard) return;
    navigator.clipboard
      .writeText(artifact.source)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => undefined);
  }

  const Title = asSheet ? SheetTitle : "h2";
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="artifact-panel">
      <div className="flex items-center gap-1 border-b border-border px-3 py-2">
        <div className="min-w-0 flex-1">
          <Title className="truncate text-sm font-semibold text-foreground">{artifact.title}</Title>
          {asSheet ? (
            <SheetDescription className="truncate text-xs">{kindLabel(artifact)}</SheetDescription>
          ) : (
            <p className="truncate text-xs text-muted-foreground">{kindLabel(artifact)}</p>
          )}
        </div>
        {versions.length > 1 && (
          <span className="inline-flex items-center text-xs text-muted-foreground">
            <button
              type="button"
              className={ICON_BUTTON}
              aria-label={t("Previous version", "Vorherige Version")}
              disabled={index === 0}
              onClick={() => onSelect(versions[index - 1])}
            >
              <ChevronLeft className="size-4" aria-hidden />
            </button>
            <span className="font-mono tabular-nums" aria-live="polite">
              {index + 1}/{versions.length}
            </span>
            <button
              type="button"
              className={ICON_BUTTON}
              aria-label={t("Next version", "Nächste Version")}
              disabled={index === versions.length - 1}
              onClick={() => onSelect(versions[index + 1])}
            >
              <ChevronRight className="size-4" aria-hidden />
            </button>
          </span>
        )}
        <button
          type="button"
          className={ICON_BUTTON}
          onClick={copy}
          aria-label={copied ? t("Copied", "Kopiert") : t("Copy", "Kopieren")}
        >
          {copied ? (
            <Check className="size-4" aria-hidden />
          ) : (
            <Copy className="size-4" aria-hidden />
          )}
        </button>
        <button
          type="button"
          className={ICON_BUTTON}
          onClick={() => download(artifact)}
          disabled={!artifact.closed}
          aria-label={t(
            `Download ${fileNameFor(artifact)}`,
            `${fileNameFor(artifact)} herunterladen`,
          )}
        >
          <Download className="size-4" aria-hidden />
        </button>
        {/* The sheet brings its own close button. */}
        {!asSheet && (
          <button
            type="button"
            className={ICON_BUTTON}
            onClick={onClose}
            aria-label={t("Close", "Schließen")}
          >
            <X className="size-4" aria-hidden />
          </button>
        )}
      </div>
      <Tabs
        value={shownTab}
        onValueChange={(v) => setTab(v as "preview" | "code")}
        className="flex min-h-0 flex-1 flex-col"
      >
        {previewable && (
          <TabsList className="mx-3 mt-2 self-start">
            <TabsTrigger value="preview">{t("Preview", "Vorschau")}</TabsTrigger>
            <TabsTrigger value="code">{t("Code", "Code")}</TabsTrigger>
          </TabsList>
        )}
        {previewable && (
          <TabsContent
            value="preview"
            className={cn(
              "mt-2 min-h-0 flex-1 overflow-auto bg-background",
              artifact.kind === "html" && "overflow-hidden",
            )}
          >
            <Preview key={`${artifact.id}:${dark}`} artifact={artifact} mode={mode} />
          </TabsContent>
        )}
        <TabsContent value="code" className="mt-2 min-h-0 flex-1 overflow-auto">
          {artifact.kind === "code" && artifact.closed && isPythonLanguage(artifact.language) && (
            <div className="border-b border-border px-3 py-2">
              <PythonRun code={artifact.source} mode={mode} />
            </div>
          )}
          <Source artifact={artifact} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

export function ArtifactPanel({
  artifact,
  versions,
  mode = "scripts",
  onSelect,
  onClose,
  asSheet,
}: {
  artifact: Artifact | null;
  versions: Artifact[];
  /** How the preview may run (see useArtifactsPanel). */
  mode?: PreviewMode;
  onSelect: (artifact: Artifact) => void;
  onClose: () => void;
  /** A sheet over the chat instead of a column beside it. */
  asSheet: boolean;
}) {
  // Beside the chat (the sheet does this itself): focus moves into the panel
  // when it opens and goes back to where it was when it closes.
  const aside = useRef<HTMLElement>(null);
  const returnTo = useRef<HTMLElement | null>(null);
  const open = !!artifact && !asSheet;
  useEffect(() => {
    if (!open) return;
    returnTo.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    aside.current?.focus();
    return () => {
      const back = returnTo.current;
      if (back?.isConnected) back.focus();
    };
  }, [open]);
  if (asSheet) {
    return (
      <Sheet open={!!artifact} onOpenChange={(open) => !open && onClose()}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-2xl">
          {artifact && (
            <PanelBody
              artifact={artifact}
              versions={versions}
              mode={mode}
              onSelect={onSelect}
              onClose={onClose}
              asSheet
            />
          )}
        </SheetContent>
      </Sheet>
    );
  }
  if (!artifact) return null;
  return (
    <aside
      ref={aside}
      tabIndex={-1}
      aria-label={artifact.title}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onClose();
        }
      }}
      className="flex w-[clamp(24rem,42vw,45rem)] shrink-0 flex-col border-l border-border bg-background focus-visible:outline-none"
    >
      <PanelBody
        artifact={artifact}
        versions={versions}
        mode={mode}
        onSelect={onSelect}
        onClose={onClose}
        asSheet={false}
      />
    </aside>
  );
}
