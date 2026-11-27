// The artifacts of an answer (spec §5.2): fenced blocks that stand on their
// own and open beside the chat -- ```html, ```svg, ```mermaid, and any other
// block of MIN_CODE_LINES lines or more. They are read from the message's
// content when it renders: no message format of their own, so old chats,
// exports and replays have them too.

export type ArtifactKind = "html" | "svg" | "mermaid" | "code";

export type Artifact = {
  /** `${messageId}:${index}`, the index counting this message's artifacts. */
  id: string;
  kind: ArtifactKind;
  /** The fence's language as written ("" without one). */
  language: string;
  title: string;
  source: string;
  /** False while the block's closing fence has not arrived (streaming). */
  closed: boolean;
  messageId: string;
};

/** From this many lines on, a code block in any language is an artifact. */
export const MIN_CODE_LINES = 15;

const TITLE_MAX = 80;
/** How far down a heading or a comment still names the block. */
const TITLE_LINES = 5;

const OPEN_FENCE = /^([ \t]*)(`{3,}|~{3,})(.*)$/;
const CLOSE_FENCE = /^[ \t]*(`{3,}|~{3,})[ \t]*$/;

function kindOf(language: string, lineCount: number): ArtifactKind | null {
  const lang = language.toLowerCase();
  if (lang === "html" || lang === "htm") return "html";
  if (lang === "svg") return "svg";
  if (lang === "mermaid") return "mermaid";
  // KaTeX's formula blocks (see chat-markdown.tsx) are no code.
  if (lang === "math" || lang === "math-source") return null;
  return lineCount >= MIN_CODE_LINES ? "code" : null;
}

/** A line without the fence's own indentation (as CommonMark reads it). */
function unindent(line: string, indent: number): string {
  let i = 0;
  while (i < indent && (line[i] === " " || line[i] === "\t")) i += 1;
  return line.slice(i);
}

const ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
};

/** Markup as the text it shows: tags dropped, the common entities read. */
function plainText(markup: string): string {
  return markup
    .replace(/<[^>]*>/g, "")
    .replace(/&(amp|lt|gt|quot|apos|nbsp);/g, (_, name: string) => ENTITIES[name])
    .replace(/\s+/g, " ")
    .trim();
}

function capped(title: string): string {
  return title.length > TITLE_MAX ? `${title.slice(0, TITLE_MAX - 1).trimEnd()}…` : title;
}

const COMMENT = /^(?:\/\/+|--|%%|;+|\/\*+|<!--)\s*(.*?)\s*(?:\*+\/|-->)?$/;

/** `<title>`, else the first heading, else a comment at the top, else the
 *  language. */
function titleOf(kind: ArtifactKind, language: string, source: string): string {
  const title = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(source)?.[1];
  if (title && plainText(title)) return capped(plainText(title));
  const top = source
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, TITLE_LINES);
  for (const line of top) {
    const heading = /^#{1,6}\s+(.+)$/.exec(line)?.[1];
    if (heading?.trim()) return capped(heading.trim());
  }
  if (kind === "html" || kind === "svg") {
    const h1 = /<h1[^>]*>([\s\S]*?)<\/h1>/i.exec(source)?.[1];
    if (h1 && plainText(h1)) return capped(plainText(h1));
  }
  for (const line of top) {
    const comment = COMMENT.exec(line)?.[1];
    if (comment) return capped(comment);
  }
  return language || "code";
}

/** The artifacts of one message, in the order they appear. */
export function extractArtifacts(content: string, messageId: string): Artifact[] {
  const lines = content.split(/\r?\n/);
  const found: Artifact[] = [];
  let i = 0;
  while (i < lines.length) {
    const open = OPEN_FENCE.exec(lines[i]);
    // A backtick fence's info string holds no backtick (that is inline code).
    if (!open || (open[2][0] === "`" && open[3].includes("`"))) {
      i += 1;
      continue;
    }
    const [, indent, fence, info] = open;
    const language = info.trim().split(/\s+/)[0] ?? "";
    const body: string[] = [];
    let closed = false;
    let j = i + 1;
    for (; j < lines.length; j += 1) {
      const close = CLOSE_FENCE.exec(lines[j]);
      if (close && close[1][0] === fence[0] && close[1].length >= fence.length) {
        closed = true;
        break;
      }
      body.push(unindent(lines[j], indent.length));
    }
    const kind = kindOf(language, body.length);
    if (kind) {
      const source = body.join("\n");
      found.push({
        id: `${messageId}:${found.length}`,
        kind,
        language,
        title: titleOf(kind, language, source),
        source,
        closed,
        messageId,
      });
    }
    i = j + 1;
  }
  return found;
}

/** Every artifact of the messages on the visible path, oldest first. */
export function artifactsOnPath(messages: { id: string; content: string }[]): Artifact[] {
  return messages.flatMap((message) => extractArtifacts(message.content, message.id));
}

/** The versions of an artifact: those on the path with its title, oldest
 *  first. One not on the path (a live answer) is its own only version. */
export function versionsOf(artifact: Artifact, onPath: Artifact[]): Artifact[] {
  const versions = onPath.filter((a) => a.title === artifact.title);
  return versions.some((a) => a.id === artifact.id) ? versions : [artifact];
}

/** What the card and the panel call the artifact's kind. */
export function kindLabel(artifact: Artifact): string {
  if (artifact.kind === "html") return "HTML";
  if (artifact.kind === "svg") return "SVG";
  if (artifact.kind === "mermaid") return "Mermaid";
  return artifact.language || "Code";
}

const EXTENSIONS: Record<string, string> = {
  python: "py",
  py: "py",
  javascript: "js",
  js: "js",
  jsx: "jsx",
  typescript: "ts",
  ts: "ts",
  tsx: "tsx",
  json: "json",
  sql: "sql",
  bash: "sh",
  sh: "sh",
  shell: "sh",
  zsh: "sh",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  css: "css",
  markdown: "md",
  md: "md",
  csv: "csv",
  xml: "xml",
  java: "java",
  go: "go",
  rust: "rs",
  ruby: "rb",
  php: "php",
  c: "c",
  cpp: "cpp",
  csharp: "cs",
  cs: "cs",
};

/** A download name: the title as a slug, with the kind's extension. */
export function fileNameFor(artifact: Artifact): string {
  const slug =
    artifact.title
      .normalize("NFKD")
      .toLowerCase()
      .replace(/[^\w\s-]/g, "")
      .trim()
      .replace(/[\s_-]+/g, "-")
      .slice(0, 60)
      .replace(/-+$/, "") || "artifact";
  const extension =
    artifact.kind === "code"
      ? (EXTENSIONS[artifact.language.toLowerCase()] ?? "txt")
      : artifact.kind === "mermaid"
        ? "mmd"
        : artifact.kind;
  return `${slug}.${extension}`;
}
