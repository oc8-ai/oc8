// Markdown that is still being written. Two jobs, both pure:
//
// - `splitBlocks` cuts an answer into top-level blocks at blank lines, so
//   the blocks already finished can be rendered once and kept (stable keys,
//   memoised) while only the last one changes as words arrive.
// - `stabilizeTail` makes the unfinished end of an answer render the way it
//   will once it is finished, so nothing jumps when the closing marker lands:
//   an open `**` or `` ` `` is closed early, a half-typed link shows only its
//   words, a table waits for its header rule, and a line that is only
//   markdown punctuation ("-", "#", "``") waits for its first word.
//
// A line starting with `$$` opens a math block the way a code fence does,
// unless the line closes it again (a one-line `$$x$$` is inline math); the
// next `$$`, wherever on its line, closes it -- as ChatMarkdown's
// `mathInProse` reads it. Neither job looks inside one.

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const MATH_OPEN = /^ {0,3}\$\$/;
// A list item, so a loose list (items apart by a blank line) stays one list.
const LIST_ITEM = /^([-*+]|\d{1,9}[.)])(\s|$)/;
// Reference-style definitions apply to the whole document; a text that has
// any is never split.
const DEFINITION = /^ {0,3}\[[^\]]+\]:\s/m;

function isBlank(line: string): boolean {
  return line.trim() === "";
}

/** Top-level blocks: each keeps its trailing blank lines, so a block's text
 *  does not change once the next one has started. Joined they give `text`. */
export function splitBlocks(text: string): string[] {
  if (!text) return [];
  if (DEFINITION.test(text)) return [text];
  const lines = text.split("\n");
  const blocks: string[] = [];
  let current: string[] = [];
  let fence: string | null = null;
  let prevBlank = false;
  lines.forEach((line, i) => {
    const startsBlock =
      i > 0 &&
      fence === null &&
      prevBlank &&
      !isBlank(line) &&
      !/^\s/.test(line) &&
      !LIST_ITEM.test(line);
    if (startsBlock && current.length > 0) {
      blocks.push(current.join("\n") + "\n");
      current = [];
    }
    current.push(line);
    fence = nextFence(fence, line);
    prevBlank = isBlank(line);
  });
  blocks.push(current.join("\n"));
  return blocks;
}

/** The fence open after `line`, given the one open before it (`$` for
 *  math, else the code fence's character). */
function nextFence(open: string | null, line: string): string | null {
  if (open === "$") return line.includes("$$") ? null : open;
  const marker = FENCE.exec(line)?.[1];
  if (open !== null) return marker && marker[0] === open ? null : open;
  if (marker) return marker[0];
  if (MATH_OPEN.test(line) && !line.slice(line.indexOf("$$") + 2).includes("$$")) return "$";
  return null;
}

interface OpenFence {
  marker: string;
  line: number;
  /** Offset of its marker. */
  at: number;
}

/** The fence (code or `$$` math) left open at the end of `text`, and where
 *  the last fence closed before it ends (0 when none did). */
function scanFences(text: string): { open: OpenFence | null; closedAt: number } {
  let open: OpenFence | null = null;
  let closedAt = 0;
  let offset = 0;
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const next = nextFence(open?.marker ?? null, line);
    if (open === null && next !== null) {
      open = { marker: next, line: i, at: offset + line.length - line.trimStart().length };
    } else if (open !== null && next === null) {
      open = null;
      closedAt = offset + line.length;
    }
    offset += line.length + 1;
  }
  return { open, closedAt };
}

function openFence(text: string): OpenFence | null {
  return scanFences(text).open;
}

/** Where the code fence left open at the end of `text` starts (its marker),
 *  or null: that block is still being written. */
export function openCodeFenceAt(text: string): number | null {
  const open = openFence(text);
  return open && open.marker !== "$" ? open.at : null;
}

/** A table row's cell count. */
function cells(line: string): number {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").length;
}

const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const PUNCTUATION_ONLY = /^[\s`~*_\-=#>|+:.]+$/;

/** Closes what the end of `para` left open: inline code and `**`/`~~`. A
 *  marker with nothing after it yet is dropped instead of closed. */
function closeInline(para: string): string {
  const stack: { marker: string; at: number }[] = [];
  let code: { run: number; at: number } | null = null;
  let i = 0;
  while (i < para.length) {
    const ch = para[i];
    if (ch === "\\") {
      i += 2;
      continue;
    }
    if (ch === "`") {
      let run = 1;
      while (para[i + run] === "`") run += 1;
      if (code === null) code = { run, at: i };
      else if (code.run === run) code = null;
      i += run;
      continue;
    }
    if (code !== null) {
      i += 1;
      continue;
    }
    const pair = para.slice(i, i + 2);
    if (pair === "**" || pair === "~~") {
      if (stack.length > 0 && stack[stack.length - 1].marker === pair) stack.pop();
      else stack.push({ marker: pair, at: i });
      i += 2;
      continue;
    }
    i += 1;
  }
  let out = para;
  const closers: string[] = [];
  if (code !== null) {
    const { run, at } = code;
    if (out.slice(at + run).trim() === "") out = out.slice(0, at);
    else closers.push("`".repeat(run));
    // Markers opened inside the code span are code, not emphasis.
    while (stack.length > 0 && stack[stack.length - 1].at > at) stack.pop();
  }
  for (let k = stack.length - 1; k >= 0; k -= 1) {
    const { marker, at } = stack[k];
    if (out.slice(at + marker.length).trim() === "") out = out.slice(0, at);
    else closers.push(marker);
  }
  if (closers.length === 0) return out;
  // A closing `**` after a space is not one: close right after the last word.
  return out.trimEnd() + closers.join("");
}

/** A half-typed link shows its words: `[text](https://exa` -> `text`. */
function settleLink(para: string): string {
  return para.replace(/!?\[([^\]\n]*)\]\([^)\s]*$/, "$1").replace(/!?\[([^\]\n]*)$/, "$1");
}

/** The unfinished end of a streaming answer, made to render as it will
 *  once finished. Only for the last block while it streams. */
export function stabilizeTail(text: string): string {
  if (!text) return text;
  const open = openFence(text);
  if (open?.marker === "$") return asMathSource(text, open.line, "$$");
  if (open) return text;
  const bracket = openDisplayBracket(text);
  if (bracket !== null) return asMathSource(text, bracket, "\\[");
  let lines = text.split("\n");
  // A line that is only markdown punctuation is a marker without its word
  // yet ("-", "##", "``", "--" under a line would make it a heading).
  const last = lines[lines.length - 1];
  if (
    last !== "" &&
    // The closing line of a fence is no marker waiting for its word.
    scanFences(text).closedAt !== text.length &&
    PUNCTUATION_ONLY.test(last) &&
    !(last.includes("|") && TABLE_RULE.test(last))
  ) {
    lines = lines.slice(0, -1);
  }
  // Table rows wait for the header rule: before it they are a paragraph of
  // pipes that turns into a table a moment later.
  let rows = 0;
  while (rows < lines.length && /^\s*\|/.test(lines[lines.length - 1 - rows])) rows += 1;
  const table = lines.slice(lines.length - rows);
  const ruled = rows > 1 && TABLE_RULE.test(table[1]) && cells(table[1]) === cells(table[0]);
  if (rows > 0 && !ruled) {
    lines = lines.slice(0, lines.length - rows);
  }
  // A lone `$` at the very end may be a formula's closing one or a price's
  // ("$5-$" before "10"): it waits for the next character to tell.
  let out = lines.join("\n").replace(/(^|[^$\\])\$$/, "$1");
  // The paragraph being written: after the last blank line and after the
  // last closed fence (code or math).
  let start = 0;
  const blank = /\n[ \t]*\n/g;
  for (let m = blank.exec(out); m; m = blank.exec(out)) start = m.index + m[0].length;
  start = Math.max(start, scanFences(out).closedAt);
  const head = out.slice(0, start);
  const para = closeInline(settleLink(out.slice(start)));
  out = head + para;
  return out;
}

/** A formula still being written shows as its source, a ```math-source
 *  block (a plain <pre>, see ChatMarkdown; ```math would go to KaTeX),
 *  until its closing marker lands: half a
 *  formula would not parse. The opening marker on line `at` becomes the
 *  fence; TeX after it on that line moves to the next. */
function asMathSource(text: string, at: number, marker: string): string {
  const lines = text.split("\n");
  const line = lines[at];
  const indent = /^[ \t]*/.exec(line)?.[0] ?? "";
  const rest = line.slice(line.indexOf(marker) + marker.length).trim();
  lines[at] = `${indent}\`\`\`math-source` + (rest ? `\n${indent}${rest}` : "");
  return lines.join("\n");
}

/** The line of a `\[` that starts a line outside code and is not closed
 *  yet, or null. A `\[` inside a fenced block is code: turned into math
 *  source it would break the block open. */
function openDisplayBracket(text: string): number | null {
  const lines = text.split("\n");
  let candidate: number | null = null;
  let fence: string | null = null;
  for (let i = 0; i < lines.length; i += 1) {
    if (fence === null && /^ {0,3}\\\[/.test(lines[i])) candidate = i;
    fence = nextFence(fence, lines[i]);
  }
  if (candidate === null) return null;
  return lines.slice(candidate).join("\n").includes("\\]") ? null : candidate;
}

/** Longer than this, what stands before a `$$` on its line is prose. */
const MAX_LEAD = 200;

/** The text of the last line of `out`, or null when it is longer than
 *  `MAX_LEAD` (read backwards to its newline once; no regex over `out`). */
export function lastLine(out: string): string | null {
  const start = out.lastIndexOf("\n") + 1;
  return out.length - start > MAX_LEAD ? null : out.slice(start);
}

function isBlankChar(ch: string | undefined): boolean {
  return ch === " " || ch === "\t";
}

/** What may stand before a `$$` that opens a display formula: blanks, or the
 *  markers of the list items and quotes it sits in ("- ", "1. ", "> - ").
 *  Returns the prefix that keeps a line inside those containers (a list
 *  marker becomes blanks of its width, a quote's `>` stays), or undefined
 *  when `lead` is prose. One pass over at most `MAX_LEAD` characters. */
export function continuationOf(lead: string | null): string | undefined {
  if (lead === null || lead.length > MAX_LEAD) return undefined;
  let prefix = "";
  let i = 0;
  while (i < lead.length) {
    const ch = lead[i];
    if (isBlankChar(ch)) {
      prefix += ch;
      i += 1;
      continue;
    }
    if (ch === ">") {
      prefix += ">";
      i += 1;
      if (isBlankChar(lead[i])) {
        prefix += lead[i];
        i += 1;
      }
      continue;
    }
    // A list marker: `-`, `*`, `+`, or 1-9 digits and `.` or `)`; then a blank.
    let j = i;
    if (ch === "-" || ch === "*" || ch === "+") {
      j = i + 1;
    } else {
      while (j < lead.length && j - i < 9 && lead[j] >= "0" && lead[j] <= "9") j += 1;
      if (j === i || (lead[j] !== "." && lead[j] !== ")")) return undefined;
      j += 1;
    }
    if (!isBlankChar(lead[j])) return undefined;
    prefix += " ".repeat(j - i);
    i = j;
  }
  return prefix;
}

/** How many leading characters two strings share. */
export function commonPrefixLength(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a.charCodeAt(i) === b.charCodeAt(i)) i += 1;
  return i;
}
