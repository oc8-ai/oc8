// Renders an answer's markdown (headings, bold, lists, links, code, ...).
// Shared by the copilot dock, the chat window and the conversation view.
//
// react-markdown builds React elements and is given no raw-HTML plugin, so
// literal HTML in an answer stays text. Every link goes through the `a`
// renderer below, which only emits http(s) links (new tab), `#cite-n`
// citations and resolved `oc8://` object chips; anything else is plain text.
// In an answer, an http(s) link is clickable only once the server has judged
// it for that saved answer (see `linkable`); a member's own words are not
// checked.
// Images are https only and load only when the member clicks to load them.
// Formulas render through KaTeX and ```mermaid blocks as diagrams (see
// `mathInProse` and MermaidBlock); both libraries are their own chunks,
// fetched the first time an answer needs them.
// In a conversation, an answer's artifacts (```html, ```svg, long code, see
// artifacts/extract.ts) show as cards that open them beside the chat; a
// ```mermaid diagram stays inline and can be opened there too.

import {
  createContext,
  memo,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { Check, Copy, ExternalLink, Image as ImageIcon } from "lucide-react";

import { ArtifactCard } from "@/components/conversation/artifacts/artifact-card";
import { useArtifacts } from "@/components/conversation/artifacts/context";
import { extractArtifacts, type Artifact } from "@/components/conversation/artifacts/extract";
import { MermaidBlock } from "@/components/conversation/mermaid-block";
import { isPythonLanguage, usePythonScope } from "@/components/conversation/python/run-context";
import { InlinePythonRun } from "@/components/conversation/python/run-output";
import { isPlainClick, objectHref, parseObjectUrl } from "@/components/conversation/object-links";
import {
  commonPrefixLength,
  continuationOf,
  lastLine,
  openCodeFenceAt,
  splitBlocks,
  stabilizeTail,
} from "@/components/markdown-stream";
import { rehypeStreamFresh } from "@/components/stream-words";
import type { ChatSourceDTO } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { browserHost, useVerifiedLinks, type LinkVerdict } from "@/lib/record-links";
import { cn } from "@/lib/utils";

function isHttp(href: string | undefined): href is string {
  return !!href && /^https?:\/\//i.test(href);
}

/** Whether an answer's http(s) href may be clickable. One the browser cannot
 *  parse never is. Otherwise it is recorded in `asked` and needs the server's
 *  verdict for this answer, read for the host this browser will open: if the
 *  server read the host differently (an IP spelling, an IDNA variant), the
 *  verdict is about another host and the link stays text. Else it is another
 *  system's link, or a connected system's that oc8 issued. No verdict yet,
 *  or none to be had, means text (fail closed). */
function linkable(
  href: string,
  verdicts: Map<string, LinkVerdict> | undefined,
  asked: Set<string>,
): boolean {
  const host = browserHost(href);
  if (!host) return false;
  asked.add(href);
  const verdict = verdicts?.get(href);
  return !!verdict && verdict.host === host && (!verdict.connected || verdict.issued);
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
  // One object per highlight: React re-applies innerHTML whenever this prop is
  // a new object, which would replace the code's nodes (and drop a selection)
  // each time the answer around it re-renders -- as it does when links verify.
  const highlighted = useMemo(() => (html ? { __html: html } : null), [html]);

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
      {highlighted ? (
        // Shiki escapes the code it highlights; its output is markup it built.
        <div
          className="overflow-x-auto px-3 py-2 font-mono text-xs [&_pre]:bg-transparent"
          dangerouslySetInnerHTML={highlighted}
        />
      ) : (
        <pre className="overflow-x-auto px-3 py-2 font-mono text-xs">{children}</pre>
      )}
    </div>
  );
}

/** Where the fence still being written starts in the block it is in (see
 *  `openCodeFenceAt`), or null: every fence in the block is closed. */
const OpenFenceAt = createContext<number | null>(null);

/** The artifacts of the answer being rendered, when it shows them as cards. */
const MessageArtifacts = createContext<Artifact[] | null>(null);

/** The answer being rendered (`artifactsOf`): a saved message's id or a live
 *  run's; Run under a Python block asks whether it may run for it. */
const MessageOf = createContext<string | undefined>(undefined);

/** The artifact a fenced block is: the one with its language and code, else
 *  -- for the fence still being written, which the stream may show a little
 *  behind, or with the start of its closing fence -- the last one of its
 *  language that what is shown starts, or that starts it. */
function artifactOf(
  artifacts: Artifact[] | null,
  language: string | undefined,
  code: string,
  open: boolean,
): Artifact | null {
  if (!artifacts) return null;
  const same = artifacts.find((a) => a.source === code && a.language === (language ?? ""));
  if (same) return same;
  if (!open) return null;
  const lang = language ?? "";
  return (
    [...artifacts]
      .reverse()
      .find(
        (a) => a.language === lang && (a.source.startsWith(code) || code.startsWith(a.source)),
      ) ?? null
  );
}

type HastNode = { position?: { start: { offset?: number }; end: { offset?: number } } };

/** A fenced block: a diagram for ```mermaid, a formula's source while it is
 *  written or KaTeX loads, else highlighted code. */
function FencedBlock({ node, children }: { node?: HastNode; children?: ReactNode }) {
  const openAt = useContext(OpenFenceAt);
  const artifacts = useContext(MessageArtifacts);
  const panel = useArtifacts();
  const python = usePythonScope();
  const messageOf = useContext(MessageOf);
  const { language, code } = codeOf(children);
  const start = node?.position?.start.offset;
  const end = node?.position?.end.offset;
  const open =
    openAt !== null && start !== undefined && end !== undefined && start <= openAt && openAt < end;
  const artifact = panel ? artifactOf(artifacts, language, code, open) : null;
  if (language === "mermaid") {
    return (
      <MermaidBlock
        source={code}
        closed={!open}
        onOpen={artifact && panel ? () => panel.open(artifact) : undefined}
      />
    );
  }
  if (artifact) return <ArtifactCard artifact={artifact} />;
  if (language === "math" || language === "math-source") {
    return <pre className="overflow-x-auto px-3 py-2 font-mono text-xs">{children}</pre>;
  }
  if (python && messageOf && !open && isPythonLanguage(language)) {
    return (
      <div className="space-y-1">
        <CodeBlock>{children}</CodeBlock>
        <InlinePythonRun code={code} messageId={messageOf} />
      </div>
    );
  }
  return <CodeBlock>{children}</CodeBlock>;
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
// there is code (an index), never a citation; and a dollar there is no math.
const CODE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)/;
// The same, and `$$` math (after `mathInProse`, all math is): `[1]` there
// is part of a formula.
const CODE_OR_MATH = /(```[\s\S]*?(?:```|$)|`[^`\n]*`|\$\$[\s\S]*?(?:\$\$|$))/;
const BLANK_LINE = /\n[ \t]*\n/;

/** A formula's lines, each kept inside the list item or quote: a fenced
 *  block has no lazy continuation, so an unprefixed line would end it. The
 *  first line follows the prefix the caller writes. */
function insideContainer(formula: string, prefix: string): string {
  const quoted = prefix.includes(">");
  return formula
    .split("\n")
    .map((line, k) => {
      if (k === 0) return line;
      if (line.startsWith(prefix) || (quoted && line.trimStart().startsWith(">"))) return line;
      return prefix + line.trimStart();
    })
    .join("\n");
}

/** Math in prose, rewritten to the `$$` spans remark-math reads (its own
 *  single-dollar reading is off, it would take "$5 and $6" for a formula):
 *  - `$…$` is a formula only when no space is inside either dollar and no
 *    letter or digit follows the closing one: "$5-$10" stays a price, and
 *    so does the "$5" in "$5 and **$x$**";
 *  - `\(…\)` is inline math, `\[…\]` display math when it has its lines
 *    to itself, else inline;
 *  - `$$…$$` stays as it is, except that a formula over several lines that
 *    starts a line gets its `$$` lines to itself; `\$` stays a dollar.
 *  A span that is not closed stays text; none runs past a blank line. */
function mathInProse(text: string): string {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "\\") {
      const next = text[i + 1];
      if (next === "(" || next === "[") {
        const end = text.indexOf(next === "(" ? "\\)" : "\\]", i + 2);
        const inner = end === -1 ? "" : text.slice(i + 2, end);
        if (end !== -1 && inner.trim() && !BLANK_LINE.test(inner)) {
          const line = lastLine(out);
          const indent = line !== null && line.trim() === "" ? line : undefined;
          const ownLines = indent !== undefined && /^[ \t]*(?:\n|$)/.test(text.slice(end + 2));
          out +=
            next === "[" && ownLines ? `$$\n${indent}${inner.trim()}\n${indent}$$` : `$$${inner}$$`;
          i = end + 2;
          continue;
        }
      }
      out += text.slice(i, i + 2);
      i += 2;
      continue;
    }
    if (ch === "$" && text[i + 1] === "$") {
      const end = text.indexOf("$$", i + 2);
      if (end === -1) {
        out += text.slice(i);
        break;
      }
      const inner = text.slice(i + 2, end);
      const lead = lastLine(out);
      const indent = continuationOf(lead);
      if (indent !== undefined && inner.includes("\n")) {
        // A display formula with TeX on its `$$` lines: remark-math would take
        // the opening line's TeX for a fence's info string and find no
        // closing fence. Each `$$` gets a line of its own -- inside the list
        // item or quote it opened in, when it did.
        const restOfLine = /^[ \t]*(?:\n|$)/.test(text.slice(end + 2)) ? "" : `\n${indent}`;
        const body = lead?.trim() ? insideContainer(inner.trim(), indent) : inner.trim();
        out += `$$\n${indent}${body}\n${indent}$$${restOfLine}`;
      } else {
        out += text.slice(i, end + 2);
      }
      i = end + 2;
      continue;
    }
    if (ch === "$") {
      const end = text.indexOf("$", i + 1);
      const inner = end === -1 ? "" : text.slice(i + 1, end);
      const after = text[end + 1] ?? "";
      if (
        end !== -1 &&
        /^\S/.test(inner) &&
        /\S$/.test(inner) &&
        !inner.endsWith("\\") &&
        !/[\p{L}\p{N}$]/u.test(after) &&
        !BLANK_LINE.test(inner)
      ) {
        out += `$$${inner}$$`;
        i = end + 1;
        continue;
      }
    }
    out += ch;
    i += 1;
  }
  return out;
}

/** Math outside code, rewritten for remark-math (see `mathInProse`). */
function withMath(text: string): string {
  if (!/[$\\]/.test(text)) return text;
  return text
    .split(CODE)
    .map((part, i) => (i % 2 === 1 ? part : mathInProse(part)))
    .join("");
}

function withCitations(text: string, count: number): string {
  if (count === 0) return text;
  return text
    .split(CODE_OR_MATH)
    .map((part, i) =>
      i % 2 === 1
        ? part
        : part.replace(CITATION, (whole, n: string) =>
            Number(n) >= 1 && Number(n) <= count ? `[\\[${n}\\]](#cite-${n})` : whole,
          ),
    )
    .join("");
}

/** A block's markdown as it is parsed: math, then citations. */
function prepare(block: string, count: number): string {
  return withCitations(withMath(block), count);
}

/** Holds a formula outside code (prepared markdown, where all math is `$$`). */
function hasMath(markdown: string): boolean {
  return (
    markdown.includes("$$") &&
    markdown.split(CODE).some((part, i) => i % 2 === 0 && part.includes("$$"))
  );
}

const REMARK: Options["remarkPlugins"] = [
  [remarkGfm],
  [remarkMath, { singleDollarTextMath: false }],
];
const URL_AS_IS = (url: string) => url;

type RehypePlugin = NonNullable<Options["rehypePlugins"]>[number];

// rehype-katex and KaTeX's stylesheet (with its fonts, bundled): loaded once,
// the first time an answer holds a formula. Until then a formula shows as
// its source.
let katex: RehypePlugin | null = null;
let katexLoading: Promise<RehypePlugin> | null = null;

function loadKatex(): Promise<RehypePlugin> {
  katexLoading ??= Promise.all([import("rehype-katex"), import("katex/dist/katex.min.css")]).then(
    ([{ default: rehypeKatex }]) => {
      // Never throw on a formula KaTeX cannot read: it shows in red, as written.
      katex = [rehypeKatex, { throwOnError: false, strict: "ignore" }];
      return katex;
    },
    (error: unknown) => {
      katexLoading = null;
      throw error;
    },
  );
  return katexLoading;
}

/** The KaTeX rehype step once loaded; asks for it when `needed`. */
function useKatex(needed: boolean): RehypePlugin | null {
  const [plugin, setPlugin] = useState<RehypePlugin | null>(() => katex);
  useEffect(() => {
    if (!needed || plugin) return;
    let alive = true;
    loadKatex().then(
      (loaded) => {
        if (alive) setPlugin(() => loaded);
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [needed, plugin]);
  return plugin;
}

/** One top-level block. Memoised: a finished block is parsed once and its
 *  nodes stay mounted while the answer goes on below it. */
const MarkdownBlock = memo(function MarkdownBlock({
  markdown,
  components,
  freshFrom,
  math,
  openFenceAt = null,
}: {
  markdown: string;
  components: Components;
  /** Only on the block still being written: where its new words start. */
  freshFrom?: number;
  /** KaTeX's rehype step, once loaded. */
  math?: RehypePlugin | null;
  /** Only on the block still being written: its open fence (see OpenFenceAt). */
  openFenceAt?: number | null;
  /** Not read here: a new value re-renders the block when what a link may be
   *  changed, with the same renderers, so nothing in it remounts. */
  linkState?: object;
}) {
  // The fresh words are marked first: KaTeX's output carries no source
  // positions to mark, and the formula's own code node is skipped.
  const rehype: RehypePlugin[] = [];
  if (freshFrom !== undefined) rehype.push([rehypeStreamFresh, { from: freshFrom }]);
  if (math) rehype.push(math);
  return (
    <OpenFenceAt.Provider value={openFenceAt}>
      <ReactMarkdown
        remarkPlugins={REMARK}
        rehypePlugins={rehype}
        components={components}
        urlTransform={URL_AS_IS}
      >
        {markdown}
      </ReactMarkdown>
    </OpenFenceAt.Provider>
  );
});

export function ChatMarkdown({
  text,
  className,
  sources = [],
  onCitation,
  onObjectClick,
  streaming = false,
  messageId,
  memberAuthored = false,
  artifactsOf,
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
  /** The saved answer this text is. Its links are checked against it; text
   *  without one (a live answer, an activity log) shows no clickable link. */
  messageId?: string;
  /** A member's own message: links render as written, nothing is checked. */
  memberAuthored?: boolean;
  /** Show the answer's artifacts as cards (inside a conversation): the id
   *  they are numbered under -- the saved message's, or the live run's. */
  artifactsOf?: string;
}) {
  const t = useT();
  const plainLinks = !memberAuthored && (streaming || !messageId);
  // The hrefs the `a` renderer met, collected while the blocks render --
  // exactly what the parser produced, after react-markdown's own URL
  // normalisation -- and asked about once the render has committed.
  const asked = useRef(new Set<string>());
  const askedFor = useRef(messageId);
  if (askedFor.current !== messageId) {
    askedFor.current = messageId;
    asked.current = new Set();
  }
  const [toVerify, setToVerify] = useState<string[]>([]);
  const { data: verdicts } = useVerifiedLinks(memberAuthored ? undefined : messageId, toVerify);
  // The renderers read the newest callbacks and sources through a ref, so
  // the components stay the same object between renders and finished blocks
  // are not re-rendered for a new arrow function.
  const gate = { memberAuthored, plainLinks, verdicts };
  const latest = useRef({ sources, onCitation, onObjectClick, ...gate });
  latest.current = { sources, onCitation, onObjectClick, ...gate };
  const linkState = useMemo(
    () => ({ memberAuthored, plainLinks, verdicts }),
    [memberAuthored, plainLinks, verdicts],
  );
  const sourcesKey = sources.map((s) => `${s.n}:${s.title}`).join("|");
  const components = useMemo<Components>(
    () => ({
      pre: ({ node, children }) => <FencedBlock node={node}>{children}</FencedBlock>,
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
        const now = latest.current;
        if (!now.memberAuthored) {
          // Unchecked text: no link the member could follow before it is checked.
          if (now.plainLinks) return <span className="text-primary">{children}</span>;
          if (!linkable(href, now.verdicts, asked.current)) return <span>{children}</span>;
        }
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

  // The blocks render only when one of these changes, so after that commit
  // `asked` holds every href they met.
  useEffect(() => {
    const met = [...asked.current].sort();
    if (met.join("\n") !== toVerify.join("\n")) setToVerify(met);
  }, [text, components, linkState, messageId, toVerify]);

  // A new list only when an artifact changed, so the cards of finished
  // blocks are not re-rendered for every word.
  const artifactsNow = useMemo(
    () => (artifactsOf ? extractArtifacts(text, artifactsOf) : null),
    [text, artifactsOf],
  );
  const artifactsKey = artifactsNow
    ?.map((a) => `${a.id}\u0000${a.closed}\u0000${a.title}\u0000${a.source}`)
    .join("\u0001");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const artifacts = useMemo(() => artifactsNow, [artifactsKey]);

  const count = sources.length;
  const blocks = useMemo(() => splitBlocks(text), [text]);
  const last = blocks.length - 1;
  // Finished blocks are prepared once each, not again for every new word.
  const prepared = useRef<{ count: number; byBlock: Map<string, string> }>({
    count,
    byBlock: new Map(),
  });
  const done = useMemo(() => {
    const before = prepared.current.count === count ? prepared.current.byBlock : new Map();
    const byBlock = new Map<string, string>();
    const out = blocks.slice(0, -1).map((block) => {
      const markdown = before.get(block) ?? prepare(block, count);
      byBlock.set(block, markdown);
      return markdown;
    });
    prepared.current = { count, byBlock };
    return out;
  }, [blocks, count]);
  const tail =
    last >= 0 ? prepare(streaming ? stabilizeTail(blocks[last]) : blocks[last], count) : "";
  // A ```mermaid block still being written shows its source, not a diagram.
  const openFenceAt = streaming ? openCodeFenceAt(tail) : null;
  const math = useKatex(hasMath(tail) || done.some(hasMath));
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
    <MessageArtifacts.Provider value={artifacts}>
      <MessageOf.Provider value={artifactsOf}>
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
              <MarkdownBlock
                key={i}
                markdown={tail}
                components={components}
                freshFrom={freshFrom}
                math={math}
                openFenceAt={openFenceAt}
                linkState={linkState}
              />
            ) : (
              <MarkdownBlock
                key={i}
                markdown={done[i]}
                components={components}
                math={math}
                linkState={linkState}
              />
            ),
          )}
        </div>
      </MessageOf.Provider>
    </MessageArtifacts.Provider>
  );
}
