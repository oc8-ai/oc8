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
// A `$$` line opens and closes a math block the way a code fence does (a
// one-line `$$x$$` is inline math, no fence): neither job looks inside one.

const FENCE = /^ {0,3}(`{3,}|~{3,}|\$\$(?=[^$]*$))/;
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
    const marker = FENCE.exec(line)?.[1];
    if (marker) {
      if (fence === null) fence = marker[0];
      else if (marker[0] === fence) fence = null;
    }
    prevBlank = isBlank(line);
  });
  blocks.push(current.join("\n"));
  return blocks;
}

/** The fence (code or `$$` math) left open at the end of `text`: its
 *  marker's first character, its line and the offset of its marker. */
function openFence(text: string): { marker: string; line: number; at: number } | null {
  let open: { marker: string; line: number; at: number } | null = null;
  let offset = 0;
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i += 1) {
    const found = FENCE.exec(lines[i]);
    const marker = found?.[1];
    if (found && marker) {
      if (open === null) {
        open = { marker: marker[0], line: i, at: offset + found[0].length - marker.length };
      } else if (marker[0] === open.marker) open = null;
    }
    offset += lines[i].length + 1;
  }
  return open;
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
  if (open?.marker === "$") {
    // A formula still being written shows as its source, in a code block,
    // until its closing `$$` lands: half a formula would not parse.
    const lines = text.split("\n");
    lines[open.line] = lines[open.line].replace(/\$\$.*$/, "```");
    return lines.join("\n");
  }
  if (open) return text;
  let lines = text.split("\n");
  // A line that is only markdown punctuation is a marker without its word
  // yet ("-", "##", "``", "--" under a line would make it a heading).
  const last = lines[lines.length - 1];
  if (
    last !== "" &&
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
  let out = lines.join("\n");
  // The paragraph being written: after the last blank line and after the
  // last closed fence (code or math).
  let start = 0;
  const blank = /\n[ \t]*\n/g;
  for (let m = blank.exec(out); m; m = blank.exec(out)) start = m.index + m[0].length;
  const fenceLines = [...out.matchAll(/^ {0,3}(`{3,}|~{3,}|\$\$(?=[^$\n]*$)).*$/gm)];
  const lastFence = fenceLines[fenceLines.length - 1];
  if (lastFence && lastFence.index !== undefined) {
    start = Math.max(start, lastFence.index + lastFence[0].length);
  }
  const head = out.slice(0, start);
  const para = closeInline(settleLink(out.slice(start)));
  out = head + para;
  return out;
}

/** How many leading characters two strings share. */
export function commonPrefixLength(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a.charCodeAt(i) === b.charCodeAt(i)) i += 1;
  return i;
}
