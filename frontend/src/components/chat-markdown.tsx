// Renders an answer's markdown (headings, bold, lists, links, code, ...).
// Shared by the copilot dock, the chat window and the conversation view.
//
// react-markdown builds React elements and is given no raw-HTML plugin, so
// literal HTML in an answer stays text. Every link goes through the `a`
// renderer below, which only emits http(s) links (new tab), `#cite-n`
// citations and resolved `oc8://` object chips; anything else is plain text.
// Images are https only and load only when the member clicks to load them.

import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy, ExternalLink, Image as ImageIcon } from "lucide-react";

import { isPlainClick, objectHref, parseObjectUrl } from "@/components/conversation/object-links";
import { commonPrefixLength, splitBlocks, stabilizeTail } from "@/components/markdown-stream";
import { rehypeStreamFresh } from "@/components/stream-words";
import type { ChatSourceDTO } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

function isHttp(href: string | undefined): href is string {
  return !!href && /^https?:\/\//i.test(href);
}

// react-markdown hands `pre` a single `<code>` child whose className carries
// the fence info string as `language-*`; that child also holds the raw code.
function codeOf(children: ReactNode): { language: string | undefined; code: string } {
  const el = (Array.isArray(children) ? children[0] : children) as
    | { props?: { className?: string; children?: ReactNode } }
    | undefined;
  return {
    language: /language-([\w+-]+)/.exec(el?.props?.className ?? "")?.[1],
    code: String(el?.props?.children ?? "").replace(/\n$/, ""),
  };
}

const FOCUS =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

function CodeBlock({ children }: { children?: ReactNode }) {
  const t = useT();
  const { language, code } = codeOf(children);
  const [html, setHtml] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    // Shiki is its own chunk, fetched only when an answer contains code.
    void import("shiki")
      .then(({ codeToHtml }) =>
        codeToHtml(code, {
          lang: language ?? "text",
          themes: { light: "github-light", dark: "github-dark" },
          defaultColor: false,
        }),
      )
      .then((out) => {
        if (alive) setHtml(out);
      })
      .catch(() => {
        if (alive) setHtml(null);
      });
    return () => {
      alive = false;
    };
  }, [code, language]);

  function copy() {
    // navigator.clipboard is undefined outside a secure context.
    if (!navigator.clipboard) return;
    navigator.clipboard
      .writeText(code)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => undefined);
  }

  return (
    <div className="overflow-hidden rounded-md border border-border bg-muted/40">
      <div className="flex items-center justify-between px-3 py-1 text-xs text-muted-foreground">
        <span className="font-mono">{language ?? ""}</span>
        <button
          type="button"
          onClick={copy}
          className={cn(
            "inline-flex items-center gap-1 rounded-md px-1 hover:text-foreground",
            FOCUS,
          )}
          aria-label={t("Copy code", "Code kopieren")}
        >
          {copied ? (
            <Check className="size-3.5" aria-hidden />
          ) : (
            <Copy className="size-3.5" aria-hidden />
          )}
          {copied ? t("Copied", "Kopiert") : t("Copy", "Kopieren")}
        </button>
      </div>
      {html ? (
        // Shiki escapes the code it highlights; its output is markup it built.
        <div
          className="overflow-x-auto px-3 py-2 font-mono text-xs [&_pre]:bg-transparent"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : (
        <pre className="overflow-x-auto px-3 py-2 font-mono text-xs">{children}</pre>
      )}
    </div>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

/** An image the answer links to. Loading it would make the member's browser
 *  contact a host the model chose (IP, timing, a tracking pixel), so it is a
 *  button naming the host until they ask; then it loads without a referrer. */
function ExternalImage({ src, alt }: { src: string; alt: string }) {
  const t = useT();
  const [load, setLoad] = useState(false);
  const host = hostOf(src);
  if (!load) {
    return (
      <button
        type="button"
        onClick={() => setLoad(true)}
        className={cn(
          "inline-flex min-h-9 items-center gap-1 rounded-md border border-border bg-muted px-2 text-xs text-muted-foreground hover:text-foreground",
          FOCUS,
        )}
      >
        <ImageIcon className="size-3.5" aria-hidden />
        {t(`Load image from ${host}`, `Bild von ${host} laden`)}
        {alt && <span className="sr-only">: {alt}</span>}
      </button>
    );
  }
  return (
    <img
      src={src}
      alt={alt}
      referrerPolicy="no-referrer"
      loading="lazy"
      className="max-h-80 rounded-md border border-border"
    />
  );
}

const CITATION = /\[(\d{1,2})\](?!\()/g;
// Fenced blocks (closed or still streaming) and inline code spans: `[1]`
// there is code (an index), never a citation.
const CODE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)/;

function withCitations(text: string, count: number): string {
  if (count === 0) return text;
  return text
    .split(CODE)
    .map((part, i) =>
      i % 2 === 1
        ? part
        : part.replace(CITATION, (whole, n: string) =>
            Number(n) >= 1 && Number(n) <= count ? `[\\[${n}\\]](#cite-${n})` : whole,
          ),
    )
    .join("");
}

const REMARK = [remarkGfm];
const URL_AS_IS = (url: string) => url;

/** One top-level block. Memoised: a finished block is parsed once and its
 *  nodes stay mounted while the answer goes on below it. */
const MarkdownBlock = memo(function MarkdownBlock({
  markdown,
  components,
  freshFrom,
}: {
  markdown: string;
  components: Components;
  /** Only on the block still being written: where its new words start. */
  freshFrom?: number;
}) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK}
      rehypePlugins={
        freshFrom === undefined ? undefined : [[rehypeStreamFresh, { from: freshFrom }]]
      }
      components={components}
      urlTransform={URL_AS_IS}
    >
      {markdown}
    </ReactMarkdown>
  );
});

export function ChatMarkdown({
  text,
  className,
  sources = [],
  onCitation,
  onObjectClick,
  streaming = false,
}: {
  text: string;
  className?: string;
  sources?: ChatSourceDTO[];
  onCitation?: (n: number) => void;
  /** Opens an object chip's in-app route without a page load. Without it a
   *  chip is a plain link. */
  onObjectClick?: (href: string) => void;
  /** While the answer streams: a breathing dot inline after the last word,
   *  the newest words fading in, and the unfinished end rendered as it will
   *  look once finished (`stabilizeTail`). */
  streaming?: boolean;
}) {
  const t = useT();
  // The renderers read the newest callbacks and sources through a ref, so
  // the components stay the same object between renders and finished blocks
  // are not re-rendered for a new arrow function.
  const latest = useRef({ sources, onCitation, onObjectClick });
  latest.current = { sources, onCitation, onObjectClick };
  const sourcesKey = sources.map((s) => `${s.n}:${s.title}`).join("|");
  const components = useMemo<Components>(
    () => ({
      pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
      // The fresh span (rehypeStreamFresh) is keyed by where it starts, so
      // each reveal mounts a new one and its fade plays once.
      span: ({ node: _node, ...props }) => {
        const freshKey = (props as Record<string, unknown>)["data-fresh"];
        return typeof freshKey === "string" ? (
          <span key={freshKey} {...props} />
        ) : (
          <span {...props} />
        );
      },
      a: ({ href, children }) => {
        const cite = /^#cite-(\d+)$/.exec(href ?? "");
        if (cite) {
          const n = Number(cite[1]);
          const title = latest.current.sources.find((s) => s.n === n)?.title ?? "";
          return (
            <sup>
              <button
                type="button"
                onClick={() => latest.current.onCitation?.(n)}
                aria-label={t(`Source ${n}: ${title}`, `Quelle ${n}: ${title}`)}
                className={cn(
                  "rounded-md px-0.5 font-mono text-xs text-primary hover:underline",
                  FOCUS,
                )}
              >
                [{n}]
              </button>
            </sup>
          );
        }
        const object = href ? parseObjectUrl(href) : null;
        if (object) {
          const target = objectHref(object.kind, object.id);
          if (!target) return <span>{children}</span>;
          return (
            <a
              href={target}
              onClick={(e) => {
                const open = latest.current.onObjectClick;
                if (!open || !isPlainClick(e)) return;
                e.preventDefault();
                open(target);
              }}
              className={cn(
                "inline-flex items-center gap-1 rounded-full border border-border bg-muted px-2 text-xs text-foreground no-underline hover:bg-muted/70",
                FOCUS,
              )}
            >
              {children}
            </a>
          );
        }
        if (!isHttp(href)) return <span>{children}</span>;
        return (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              // Inline, so a long URL wraps instead of widening the column.
              "inline rounded-md text-primary underline",
              FOCUS,
            )}
          >
            {children}
            <ExternalLink className="ml-0.5 inline size-3.5 align-text-bottom" aria-hidden />
            <span className="sr-only">{t(" (opens in a new tab)", " (öffnet in neuem Tab)")}</span>
          </a>
        );
      },
      img: ({ src, alt }) =>
        typeof src === "string" && src.startsWith("https://") ? (
          <ExternalImage src={src} alt={alt ?? ""} />
        ) : (
          <span className="text-muted-foreground">{alt}</span>
        ),
    }),
    // The renderers read sources through `latest`, but a citation's label
    // names its source: new sources must re-render the blocks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t, sourcesKey],
  );

  const count = sources.length;
  const blocks = useMemo(() => splitBlocks(text), [text]);
  const last = blocks.length - 1;
  const tail =
    last >= 0 ? withCitations(streaming ? stabilizeTail(blocks[last]) : blocks[last], count) : "";
  // Where the newest words of the last block start, against what the last
  // commit showed. Read from a ref the effect below writes after commit, so
  // a second render of the same props (StrictMode) gets the same answer.
  const shown = useRef<{ index: number; markdown: string; from: number }>({
    index: -1,
    markdown: "",
    from: 0,
  });
  const prev = shown.current;
  const freshFrom = !streaming
    ? undefined
    : prev.index === last
      ? prev.markdown === tail
        ? prev.from
        : commonPrefixLength(prev.markdown, tail)
      : 0;
  useEffect(() => {
    shown.current = { index: last, markdown: tail, from: freshFrom ?? tail.length };
  }, [last, tail, freshFrom]);

  return (
    <div
      data-testid={streaming ? "stream-tail" : undefined}
      className={cn(
        streaming && "stream-tail",
        "min-w-0 space-y-2 text-sm leading-relaxed text-foreground [overflow-wrap:anywhere] [&_code]:rounded-md [&_code]:bg-muted [&_code]:px-1 [&_code]:font-mono [&_code]:text-xs [&_h1]:text-base [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-semibold [&_li]:ml-4 [&_ol]:list-decimal [&_ol]:space-y-0.5 [&_strong]:font-semibold [&_ul]:list-disc [&_ul]:space-y-0.5 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_table]:block [&_table]:w-full [&_table]:overflow-x-auto [&_table]:border-collapse [&_th]:border [&_th]:border-border [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-medium [&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1",
        className,
      )}
    >
      {blocks.map((block, i) =>
        i === last ? (
          <MarkdownBlock key={i} markdown={tail} components={components} freshFrom={freshFrom} />
        ) : (
          <MarkdownBlock key={i} markdown={withCitations(block, count)} components={components} />
        ),
      )}
    </div>
  );
}
